from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import packager
from .errors import APIError, ConfigError, FileError
from .models import StageEvent
from .providers import minimax

# ------------------------------------------------------------ 生命周期状态
PROCESSING = "processing"    # 已提交，服务端处理中
SUCCESS = "success"          # 服务端完成，待下载（结果文件约 9 小时后过期）
DOWNLOADING = "downloading"  # 下载打包中（中间态，防重入）
PACKAGED = "packaged"        # 已下载+解包+落盘+tar（终态）
FAILED = "failed"            # 服务端失败（终态，可重试）
EXPIRED = "expired"          # 服务端过期（终态，可重试）
LOST = "lost"                # 查询返回任务不存在（可重试）

TERMINAL_STATUSES = (PACKAGED, FAILED, EXPIRED)
ALL_STATUSES = (PROCESSING, SUCCESS, DOWNLOADING, PACKAGED, FAILED, EXPIRED, LOST)

STATUS_LABELS = {
    PROCESSING: "处理中",
    SUCCESS: "待下载",
    DOWNLOADING: "下载中",
    PACKAGED: "已完成",
    FAILED: "失败",
    EXPIRED: "已过期",
    LOST: "记录丢失",
}

# MiniMax 结果文件有效期 9 小时；超过 8 小时未下载给出过期警告
EXPIRY_WARN_SECONDS = 8 * 3600


@dataclass
class TaskRecord:
    """一次全量批量转换任务的持久化记录（一次任务可含多种语言）。"""

    task_id: str
    provider: str
    languages: List[str]
    entry_map: Dict[str, str]                 # 目录码(d0001) -> 语言
    entries: List[Dict[str, str]]             # [{"dir": 码, "voice_id": 语音ID, "text": 文本}]（重试快照）
    entry_count: int
    created_at: float
    updated_at: float
    output_dir: str
    status: str = PROCESSING
    result_file_id: Optional[str] = None
    upload_file_id: Optional[int] = None
    usage_characters: int = 0
    attempts: int = 1
    last_error: Optional[str] = None
    # 打包后按语言的结果：{语言: {"success": [...], "missing": [...], "tar_path": str|None}}
    language_results: Dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def build_zip_entries(rec_entries: List[Dict[str, str]]) -> List[Tuple[str, str]]:
    """把记录词条快照转为 zip 成员列表：d<码><语音ID>.txt（真实 API 验证的命名方案）。"""
    return [(f"{e['dir']}{e['voice_id']}.txt", e["text"]) for e in rec_entries]


class TaskStore:
    """tasks.json 持久化：原子写（tmp+os.replace），单 Lock，损坏时备份重建。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._records: Dict[str, TaskRecord] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("tasks", []):
                rec = TaskRecord.from_dict(item)
                self._records[rec.task_id] = rec
        except (OSError, ValueError, TypeError):
            # 文件损坏：备份后从空开始
            try:
                os.replace(self.path, self.path + f".corrupt-{int(time.time())}.bak")
            except OSError:
                pass
            self._records = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        payload = {"version": 1,
                   "tasks": [r.to_dict() for r in
                             sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)]}
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            raise FileError(f"无法写入任务记录文件 {self.path}: {e}")

    def list(self) -> List[TaskRecord]:
        """全部记录，新的在前。"""
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._records.get(str(task_id))

    def upsert(self, rec: TaskRecord) -> None:
        rec.updated_at = time.time()
        with self._lock:
            self._records[rec.task_id] = rec
            self._save()

    def remove(self, task_id: str) -> bool:
        with self._lock:
            if str(task_id) not in self._records:
                return False
            del self._records[str(task_id)]
            self._save()
            return True


# ------------------------------------------------------------ 生命周期操作
def _require_minimax(rec: TaskRecord) -> None:
    if rec.provider != "minimax":
        raise ConfigError(f"任务记录供应商不受支持: {rec.provider}（目前仅支持 minimax）")


def needs_refresh(rec: TaskRecord) -> bool:
    """非终态且非下载中的记录需要刷新。"""
    return rec.status in (PROCESSING, SUCCESS, LOST) or (
        rec.status == SUCCESS and not rec.result_file_id)


def expiry_risk(rec: TaskRecord, now: Optional[float] = None) -> bool:
    """已成功但长时间未下载（结果文件约 9 小时过期，8 小时起告警）。"""
    if rec.status != SUCCESS:
        return False
    return (now if now is not None else time.time()) - rec.created_at > EXPIRY_WARN_SECONDS


def refresh_record(store: TaskStore, rec: TaskRecord, pcfg: dict) -> bool:
    """单次刷新任务状态。返回状态是否发生变化；查询失败记 last_error 返回 False。"""
    _require_minimax(rec)
    try:
        status, fid = minimax.query_task(rec.task_id, pcfg)
    except APIError as e:
        msg = str(e)
        if "not found" in msg.lower() or "不存在" in msg:
            rec.status = LOST
            rec.last_error = msg
            store.upsert(rec)
            return True
        rec.last_error = msg
        store.upsert(rec)
        return False
    changed = False
    if status == "success":
        if rec.status != SUCCESS:
            changed = True
        rec.status = SUCCESS
        if fid:
            rec.result_file_id = fid
        rec.last_error = None
    elif status == "failed":
        changed = rec.status != FAILED
        rec.status = FAILED
        rec.last_error = "服务端任务处理失败"
    elif status == "expired":
        changed = rec.status != EXPIRED
        rec.status = EXPIRED
        rec.last_error = "服务端任务已过期"
    else:
        changed = rec.status != PROCESSING
        rec.status = PROCESSING
        rec.last_error = None
    store.upsert(rec)
    return changed


def refresh_records(store: TaskStore, pcfg: dict,
                    records: Optional[List[TaskRecord]] = None,
                    on_event: Optional[Callable[[StageEvent], None]] = None) -> int:
    """批量刷新（串行单查询）。返回状态发生变化的记录数。终态记录跳过（防 packaged 被查询回退）。"""
    targets = records if records is not None else [r for r in store.list() if needs_refresh(r)]
    changed = 0
    for rec in targets:
        if rec.status in TERMINAL_STATUSES:
            continue
        if refresh_record(store, rec, pcfg):
            changed += 1
            if on_event is not None:
                on_event(StageEvent("refresh", f"任务 {rec.task_id} -> {STATUS_LABELS.get(rec.status, rec.status)}"))
    return changed


def finalize_record(store: TaskStore, rec: TaskRecord, pcfg: dict,
                    on_event: Optional[Callable[[StageEvent], None]] = None) -> TaskRecord:
    """下载结果 tar -> 按 entry_map 分语言解包 -> 逐条落盘 -> 每语言 tar -> packaged。

    仅 success 状态可执行；downloading 中间态防重入；失败回滚状态为 success 并记 last_error。
    """
    _require_minimax(rec)
    if rec.status == PACKAGED:
        raise APIError("任务已完成打包，请勿重复操作", "api")
    if rec.status == DOWNLOADING:
        raise APIError("任务正在下载打包中", "api")
    if rec.status != SUCCESS:
        raise APIError(f"任务当前状态（{STATUS_LABELS.get(rec.status, rec.status)}）不可下载打包", "api")
    if not rec.result_file_id:
        raise APIError("任务成功但未记录结果 file_id，请先刷新", "api")

    emit = on_event or (lambda ev: None)
    rec.status = DOWNLOADING
    store.upsert(rec)
    try:
        emit(StageEvent("download", f"任务 {rec.task_id}: 开始下载结果包"))
        blob = minimax._download_tar(rec.result_file_id, pcfg)
        audio_by_code, extract_warnings = minimax.extract_named_mp3s(blob)
        emit(StageEvent("download", f"结果包 {len(blob)} 字节，解析出 {len(audio_by_code)} 条音频"))

        # 按语言分组：lang -> {vid: bytes}
        by_lang: Dict[str, Dict[str, bytes]] = {lang: {} for lang in rec.languages}
        expected_by_lang: Dict[str, List[str]] = {lang: [] for lang in rec.languages}
        for e in rec.entries:
            lang = rec.entry_map.get(e["dir"])
            if lang is None:
                continue
            expected_by_lang[lang].append(e["voice_id"])
            data = audio_by_code.get(f"{e['dir']}{e['voice_id']}")
            if data:
                by_lang[lang][e["voice_id"]] = data
                packager.write_single(lang, e["voice_id"], data, rec.output_dir)  # 逐条落盘

        results: Dict[str, dict] = {}
        for lang in rec.languages:
            rep = packager.write_language_pack(lang, by_lang[lang], expected_by_lang[lang], rec.output_dir)
            results[lang] = {"success": rep.success_ids, "missing": rep.missing_ids,
                             "tar_path": rep.tar_path}
            msg = f"{lang}: 成功 {len(rep.success_ids)}，缺失 {len(rep.missing_ids)}"
            if rep.missing_ids:
                msg += f"，缺失ID: {', '.join(rep.missing_ids)}"
            emit(StageEvent("package", msg))
        rec.language_results = results
        rec.status = PACKAGED
        rec.last_error = None
        if extract_warnings:
            rec.last_error = "；".join(extract_warnings[:5])
    except Exception as e:
        rec.status = SUCCESS  # 回滚，允许重试下载
        rec.last_error = str(e)
        store.upsert(rec)
        raise
    store.upsert(rec)
    return rec


def retry_record(store: TaskStore, rec: TaskRecord, pcfg: dict,
                 on_event: Optional[Callable[[StageEvent], None]] = None) -> TaskRecord:
    """用保存的词条快照重新提交任务：新 task_id、attempts+1、状态 processing。"""
    _require_minimax(rec)
    if rec.status not in (FAILED, EXPIRED, LOST):
        raise APIError(f"仅失败/过期/丢失的任务可重试（当前: {STATUS_LABELS.get(rec.status, rec.status)}）", "api")
    emit = on_event or (lambda ev: None)
    zip_entries = build_zip_entries(rec.entries)
    if not zip_entries:
        raise APIError("任务记录中无词条快照，无法重试", "api")
    emit(StageEvent("create", f"重新提交任务（第 {rec.attempts + 1} 次尝试，{len(zip_entries)} 条）"))
    old_task_id = rec.task_id
    tid, upload_fid, usage = minimax.submit_batch(zip_entries, pcfg)
    rec.task_id = tid
    rec.upload_file_id = upload_fid
    rec.usage_characters = usage
    rec.attempts += 1
    rec.status = PROCESSING
    rec.result_file_id = None
    rec.last_error = None
    rec.created_at = time.time()
    store.remove(old_task_id)  # 先移除旧主键，避免 store 中残留别名记录
    store.upsert(rec)
    emit(StageEvent("create", f"任务已重新创建 task_id={tid}"))
    return rec
