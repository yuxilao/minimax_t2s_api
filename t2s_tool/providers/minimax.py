from __future__ import annotations

import io
import json
import os
import re
import tarfile
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import requests

from ..errors import APIError, CancelledError, ConfigError
from ..models import CancelToken, ProviderResult, StageEvent, VoiceEntry
from . import register_provider
from .base import TTSProvider

DEFAULT_BASE_URL = "https://api.minimaxi.com"

# 文件上传 purpose（MiniMax 文件管理文档: 异步语音合成输入固定为 t2a_async_input）
DEFAULT_UPLOAD_PURPOSE = "t2a_async_input"

# 全局快速失败错误类型（此类错误会影响所有条目，立即终止整批）
FATAL_ERROR_TYPES = ("auth", "quota")


def _base(cfg: dict) -> str:
    """接口基址（含默认值）。"""
    return (cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def _check_http(resp, stage: str) -> None:
    code = resp.status_code
    if code == 200:
        return
    if code == 401:
        raise APIError(f"{stage}: 认证失败，API Key 无效或已过期", "auth", 401)
    if code == 403:
        raise APIError(f"{stage}: 访问被拒绝，权限不足", "auth", 403)
    if code == 429:
        raise APIError(f"{stage}: 请求过于频繁，配额超限，请稍后重试", "quota", 429)
    if code >= 500:
        raise APIError(f"{stage}: 服务器错误 ({code})，请稍后重试", "server", code)
    raise APIError(f"{stage}: HTTP 错误状态码 {code}", "http", code)


def _json(resp, stage: str) -> dict:
    try:
        return resp.json()
    except Exception:
        raise APIError(f"{stage}: 响应 JSON 解析失败", "parse")


def _check_base_resp(result: dict, stage: str) -> None:
    base = result.get("base_resp") or {}
    if base.get("status_code", 0) == 0:
        return
    msg = base.get("status_msg", "未知错误")
    low = str(msg).lower()
    etype = "api"
    if "quota" in low or "limit" in low or "额度" in str(msg):
        etype = "quota"
    elif "auth" in low or "key" in low or "token" in low:
        etype = "auth"
    raise APIError(f"{stage}: API 返回错误: {msg}", etype)


def _post(url, stage, timeout, **kw):
    try:
        return requests.post(url, timeout=timeout, **kw)
    except requests.exceptions.Timeout:
        raise APIError(f"{stage}: 请求超时", "timeout")
    except requests.exceptions.ConnectionError as e:
        raise APIError(f"{stage}: 网络连接失败: {e}", "network")
    except requests.exceptions.RequestException as e:
        raise APIError(f"{stage}: 请求失败: {e}", "request")


def _get(url, stage, timeout, **kw):
    try:
        return requests.get(url, timeout=timeout, **kw)
    except requests.exceptions.Timeout:
        raise APIError(f"{stage}: 请求超时", "timeout")
    except requests.exceptions.ConnectionError as e:
        raise APIError(f"{stage}: 网络连接失败: {e}", "network")
    except requests.exceptions.RequestException as e:
        raise APIError(f"{stage}: 请求失败: {e}", "request")


def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_key']}"}


def _num(cfg: dict, key: str, default):
    """数值配置读取：键缺失或显式为 None 时回退默认值（但保留合法的 0）。"""
    val = cfg.get(key)
    return default if val is None else val


def _create_task(text: str, cfg: dict) -> str:
    """创建单条异步语音合成任务（text 直传），返回 task_id。"""
    url = _base(cfg) + "/v1/t2a_async_v2"
    payload = {
        "model": cfg.get("model"),
        "text": text,
        "voice_setting": cfg.get("voice_setting"),
        "audio_setting": cfg.get("audio_setting"),
    }
    resp = _post(url, "创建任务", _num(cfg, "request_timeout", 30),
                 headers={**_headers(cfg), "Content-Type": "application/json"}, json=payload)
    _check_http(resp, "创建任务")
    result = _json(resp, "创建任务")
    _check_base_resp(result, "创建任务")
    tid = result.get("task_id")
    if not tid:
        raise APIError("创建任务: 成功但未返回 task_id", "api")
    return str(tid)


def _synthesize_sync(text: str, cfg: dict) -> bytes:
    """同步接口 /v1/t2a_v2：一次请求直接返回音频（data.audio 为 hex），无任务无轮询。"""
    url = _base(cfg) + "/v1/t2a_v2"
    payload = {
        "model": cfg.get("model"),
        "text": text,
        "voice_setting": cfg.get("voice_setting"),
        "audio_setting": cfg.get("audio_setting"),
    }
    resp = _post(url, "合成", _num(cfg, "request_timeout", 30),
                 headers={**_headers(cfg), "Content-Type": "application/json"}, json=payload)
    _check_http(resp, "合成")
    result = _json(resp, "合成")
    _check_base_resp(result, "合成")
    audio_hex = (result.get("data") or {}).get("audio")
    if not audio_hex:
        raise APIError("合成: 成功但未返回音频数据", "api")
    try:
        data = bytes.fromhex(audio_hex)
    except ValueError:
        raise APIError("合成: 音频 hex 解码失败", "parse")
    if not data:
        raise APIError("合成: 音频数据为空", "empty")
    return data


def synthesize_single(text: str, cfg: dict) -> bytes:
    """单条语音合成：固定走同步接口 /v1/t2a_v2（一次请求直接返回音频，无任务无轮询）。

    忽略 tts_mode/sync_max_chars——调用方明确要求同步链路；文本超限等错误由 API 透传。
    """
    if not text or not text.strip():
        raise APIError("合成: 文本为空", "empty")
    return _synthesize_sync(text, cfg)


def query_task(task_id: str, cfg: dict) -> Tuple[str, Optional[str]]:
    """单次查询任务状态，返回 (status小写, 成功时的file_id)。HTTP/JSON/base_resp 错误抛 APIError。"""
    url = _base(cfg) + f"/v1/query/t2a_async_query_v2?task_id={task_id}"
    resp = _get(url, "查询", _num(cfg, "request_timeout", 30), headers=_headers(cfg))
    _check_http(resp, "查询")
    result = _json(resp, "查询")
    _check_base_resp(result, "查询")
    status = str(result.get("status", "")).lower()
    fid = result.get("file_id")
    return status, (str(fid) if fid else None)


def _poll(task_id: str, cfg: dict, cancel_token: CancelToken, on_event=None) -> str:
    """轮询任务直至成功，返回 file_id；failed/expired/超时抛 APIError。"""
    interval = float(_num(cfg, "poll_interval", 1))
    timeout = float(_num(cfg, "poll_timeout", 180))
    start = time.time()
    count = 0
    while True:
        cancel_token.throw_if_cancelled()
        if time.time() - start > timeout:
            raise APIError(f"轮询: 任务查询超时（已等待 {int(timeout)} 秒）", "timeout")
        status, fid = query_task(task_id, cfg)
        count += 1
        if on_event is not None:
            waited = int(time.time() - start)
            on_event(StageEvent("poll", f"转换中，已等待 {waited} 秒", count))
        if status == "success":
            if not fid:
                raise APIError("轮询: 任务成功但未返回 file_id", "api")
            return fid
        if status == "failed":
            raise APIError("轮询: 任务处理失败（API 返回 failed 状态）", "api")
        if status == "expired":
            raise APIError("轮询: 任务已过期", "expired")
        time.sleep(interval)


def _extract_first_mp3(blob: bytes) -> bytes:
    """从单任务结果 tar 中提取第一个 .mp3 成员；非 tar 时若为 mp3 字节则原样返回。"""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:*")
    except tarfile.TarError:
        if blob[:3] == b"ID3" or (len(blob) > 2 and blob[0] == 0xFF and (blob[1] & 0xE0) == 0xE0):
            return blob
        raise APIError("下载: 结果包不是合法 tar", "parse")
    with tf:
        for m in tf.getmembers():
            if m.isfile() and m.name.lower().endswith(".mp3"):
                fobj = tf.extractfile(m)
                if fobj is None:
                    break
                data = fobj.read()
                if not data:
                    raise APIError("下载: mp3 数据为空", "empty")
                return data
    raise APIError("下载: 结果包中未找到 mp3 文件", "api")


def _download_audio(file_id: str, cfg: dict) -> bytes:
    """按 file_id 下载结果（tar 包），提取 mp3 字节。"""
    url = _base(cfg) + f"/v1/files/retrieve_content?file_id={file_id}"
    resp = _get(url, "下载", _num(cfg, "download_timeout", 60), headers=_headers(cfg))
    _check_http(resp, "下载")
    data = resp.content
    if not data:
        raise APIError("下载: 结果数据为空", "empty")
    return _extract_first_mp3(data)


def _synthesize_one(entry: VoiceEntry, cfg: dict, cancel_token: CancelToken) -> bytes:
    """单条目合成。同步模式走 /v1/t2a_v2（快）；text 超过 sync_max_chars 或强制 async 模式时走异步任务链路。"""
    cancel_token.throw_if_cancelled()
    mode = str(cfg.get("tts_mode") or "sync").lower()
    max_sync = int(_num(cfg, "sync_max_chars", 9000))
    if mode != "async" and len(entry.text) <= max_sync:
        return _synthesize_sync(entry.text, cfg)
    tid = _create_task(entry.text, cfg)
    fid = _poll(tid, cfg, cancel_token)
    return _download_audio(fid, cfg)


# ------------------------------------------------------------------ zip 批量原语
# zip 内 txt 命名方案（真实 API 验证）：禁止下划线与 CJK 文件名；输出 tar 只按 basename 命名，
# 重名会碰撞。采用 "d<NNNN><语音ID>.txt"（如 d0001Q001.txt，全局唯一、纯字母数字），
# d<NNNN> 为语言目录码，序号->语言 映射由调用方（tasks.TaskRecord.entry_map）持久化。
DIR_CODE = re.compile(r"d(\d{4})")
# 结果 tar 成员名中的目录码+语音ID（basename 优先）："..._d0001Q001/content-..._d0001Q001.mp3"
CODE_TAIL = re.compile(r"(d\d{4}Q\d+)")


def _build_zip_named(pairs: List[Tuple[str, str]]) -> str:
    """把 (zip内文件名, 文本) 列表写入 zip（utf-8），返回临时 zip 路径。"""
    work_dir = tempfile.mkdtemp(prefix="t2s_")
    zip_path = os.path.join(work_dir, "batch.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, text in pairs:
            zf.writestr(arcname, text)
    return zip_path


def _cleanup(zip_path: str) -> None:
    try:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        work_dir = os.path.dirname(zip_path)
        if os.path.isdir(work_dir) and not os.listdir(work_dir):
            os.rmdir(work_dir)
    except OSError:
        pass


def _upload(zip_path: str, cfg: dict) -> int:
    """上传 zip（purpose=t2a_async_input），返回整型 file_id。"""
    url = _base(cfg) + "/v1/files/upload"
    purpose = cfg.get("upload_purpose") or DEFAULT_UPLOAD_PURPOSE
    with open(zip_path, "rb") as f:
        files = {"file": (os.path.basename(zip_path), f, "application/zip")}
        resp = _post(url, "上传", _num(cfg, "upload_timeout", 120),
                     headers=_headers(cfg), files=files, data={"purpose": purpose})
    _check_http(resp, "上传")
    result = _json(resp, "上传")
    _check_base_resp(result, "上传")
    # 新版响应 file_id 位于 file 对象内；兼容旧版顶层 file_id
    fid = (result.get("file") or {}).get("file_id") or result.get("file_id")
    if not fid:
        raise APIError("上传: 成功但未返回 file_id", "api")
    return int(fid)


def _create_batch_task(file_id: int, cfg: dict) -> Tuple[str, int]:
    """zip 批量：text_file_id（整型）创建异步任务，一个任务转换 zip 内全部 txt。

    返回 (task_id, usage_characters)。"""
    url = _base(cfg) + "/v1/t2a_async_v2"
    payload = {
        "model": cfg.get("model"),
        "text_file_id": int(file_id),
        "voice_setting": cfg.get("voice_setting"),
        "audio_setting": cfg.get("audio_setting"),
    }
    resp = _post(url, "创建任务", _num(cfg, "request_timeout", 30),
                 headers={**_headers(cfg), "Content-Type": "application/json"}, json=payload)
    _check_http(resp, "创建任务")
    result = _json(resp, "创建任务")
    _check_base_resp(result, "创建任务")
    tid = result.get("task_id")
    if not tid:
        raise APIError("创建任务: 成功但未返回 task_id", "api")
    return str(tid), int(result.get("usage_characters") or 0)


def submit_batch(zip_entries: List[Tuple[str, str]], cfg: dict) -> Tuple[str, int, int]:
    """打包上传并创建批量任务（不轮询）。zip_entries: [(zip内文件名, 文本)]。

    返回 (task_id, upload_file_id, usage_characters)。临时 zip 总会清理。
    """
    zip_path = _build_zip_named(zip_entries)
    try:
        upload_fid = _upload(zip_path, cfg)
    finally:
        _cleanup(zip_path)
    tid, usage = _create_batch_task(upload_fid, cfg)
    return tid, upload_fid, usage


def _download_tar(file_id: str, cfg: dict) -> bytes:
    """下载批量结果 tar 原始字节。"""
    url = _base(cfg) + f"/v1/files/retrieve_content?file_id={file_id}"
    resp = _get(url, "下载", _num(cfg, "download_timeout", 120), headers=_headers(cfg))
    _check_http(resp, "下载")
    data = resp.content
    if not data:
        raise APIError("下载: 结果数据为空", "empty")
    return data


def extract_named_mp3s(blob: bytes) -> Tuple[Dict[str, bytes], List[str]]:
    """从批量结果 tar 提取全部 .mp3 成员，键为成员名中的目录码+语音ID（如 d0001Q001）。"""
    audio: Dict[str, bytes] = {}
    warnings: List[str] = []
    try:
        tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:*")
    except tarfile.TarError as e:
        raise APIError(f"解析: 结果包不是合法 tar: {e}", "parse")
    with tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.lower().endswith(".mp3"):
                continue
            basename = m.name.rsplit("/", 1)[-1]
            match = CODE_TAIL.search(basename) or CODE_TAIL.search(m.name)
            if not match:
                warnings.append(f"解析: 未知成员（无法提取条目编码）: {m.name}")
                continue
            code = match.group(1)
            if code in audio:
                warnings.append(f"解析: 条目编码重复，后者覆盖: {code}")
            fobj = tf.extractfile(m)
            if fobj is None:
                warnings.append(f"解析: 无法读取成员: {m.name}")
                continue
            data = fobj.read()
            if not data:
                warnings.append(f"解析: mp3 数据为空: {code}")
                continue
            audio[code] = data
    return audio, warnings


@register_provider
class MinimaxProvider(TTSProvider):
    name = "minimax"
    display_name = "MiniMax"

    def validate_config(self, config: dict) -> None:
        if not config.get("api_key"):
            raise ConfigError("MiniMax 配置缺少 api_key")

    def synthesize_one(self, entry: VoiceEntry, config: dict, cancel_token: CancelToken) -> bytes:
        """覆盖中间层单条接口：固定走同步接口 /v1/t2a_v2（一次请求直接返回音频，无任务无轮询）。"""
        cancel_token.throw_if_cancelled()
        return synthesize_single(entry.text, config)

    def synthesize_batch(self, entries, config, on_event, cancel_token, on_entry=None) -> ProviderResult:
        """合成一批条目。三种模式（config["tts_mode"]）：

        - "batch"（默认）：zip 打包全部条目上传，一个异步任务整批转换，按语音ID解包——
          每语言仅 1 次任务，转换完成即落盘；
        - "sync"：并发逐条同步接口（/v1/t2a_v2，短文本极快）；
        - "async"：并发逐条异步任务。

        空文本条目跳过（记 warning，计入缺失）；auth/quota 类全局错误立即终止（快速失败）。
        """
        self.validate_config(config)
        warnings = [f"空文本跳过合成: {e.voice_id}" for e in entries if e.is_empty]
        todo = [e for e in entries if not e.is_empty]
        audio: Dict[str, bytes] = {}
        total = len(todo)
        if total == 0:
            return ProviderResult(audio, warnings)

        mode = str(config.get("tts_mode") or "batch").lower()
        if mode == "batch":
            raise ConfigError("batch 模式由 pipeline 统一编排（submit_batch/query_task），"
                              "请勿直接调用 synthesize_batch")

        workers = max(1, int(_num(config, "concurrency", 5)))
        on_event(StageEvent("create", f"开始合成 {total} 条（并发 {workers}）", 0, total))

        def worker(entry: VoiceEntry) -> Tuple[str, Optional[bytes], Optional[Exception]]:
            try:
                return entry.voice_id, _synthesize_one(entry, config, cancel_token), None
            except CancelledError:
                return entry.voice_id, None, None
            except APIError as e:
                return entry.voice_id, None, e
            except Exception as e:  # 兜底：未知异常按条目降级
                return entry.voice_id, None, APIError(f"未知错误: {e}", "unknown")

        done = 0
        by_id = {e.voice_id: e for e in todo}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(worker, e) for e in todo]
            for fut in as_completed(futures):
                vid, data, err = fut.result()
                done += 1
                if isinstance(err, APIError) and err.error_type in FATAL_ERROR_TYPES:
                    cancel_token.cancel()  # 通知进行中的轮询尽快退出
                    raise err
                if data:
                    audio[vid] = data
                    if on_entry is not None:
                        on_entry(by_id[vid], data)  # 边转边落盘（单线程回调，安全）
                elif isinstance(err, APIError):
                    warnings.append(f"{vid}: {err}")
                on_event(StageEvent("download", f"已完成 {done}/{total}", done, total))
                cancel_token.throw_if_cancelled()
        return ProviderResult(audio, warnings)
