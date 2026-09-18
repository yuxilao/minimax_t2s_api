from __future__ import annotations

import os
import time
from typing import Callable, List, Optional

from . import config as config_mod
from . import packager, parsers, paths, providers, tasks as tasks_mod
from .errors import ConfigError, ParseError
from .models import CancelToken, JobReport, LanguageReport, SingleResult, StageEvent
from .providers import minimax


def _noop(ev: StageEvent) -> None:
    pass


def _select_languages(presult, languages: Optional[List[str]]):
    """languages 为 None/空列表 -> 全部语言；否则按语言名精确匹配，未知名抛 ParseError。"""
    if not languages:
        return list(presult.languages)
    by_name = {p.language: p for p in presult.languages}
    unknown = [l for l in languages if l not in by_name]
    if unknown:
        raise ParseError(f"未找到语言: {unknown}，可用: {sorted(by_name)}")
    return [by_name[l] for l in languages]


def run_job(
    input_path: str,
    parser_name: Optional[str] = "auto",
    languages: Optional[List[str]] = None,
    provider_name: Optional[str] = None,
    config: Optional[dict] = None,
    output_dir: str = "语音包",
    on_event: Optional[Callable[[StageEvent], None]] = None,
    cancel_token: Optional[CancelToken] = None,
    parsers_dir: Optional[str] = None,
    submit_only: bool = False,
    task_store: Optional[tasks_mod.TaskStore] = None,
) -> JobReport:
    """端到端编排：解析语音目录 -> 合成 -> 落盘为扁平语音包 + tar。

    batch 模式（minimax）：所有选中语言合并为一个 zip、一次 API 任务；
    submit_only=True 时提交即记录到 task_store 并返回（GUI 任务中心接管后续生命周期）。
    """
    emit = on_event or _noop
    token = cancel_token or CancelToken()
    parsers.load_parsers(parsers_dir or paths.default_parsers_dir())
    providers.load_providers()

    if parser_name in (None, "auto"):
        parser = parsers.detect_best(input_path)
    else:
        parser = parsers.get_parser(parser_name)
    presult = parser.parse(input_path)
    emit(StageEvent("parse", f"解析完成（{parser.name}）: {len(presult.languages)} 种语言"))

    pname = provider_name or (config or {}).get("provider", "minimax")
    provider = providers.get_provider(pname)
    pcfg = config_mod.get_provider_config(config or {}, pname)
    provider.validate_config(pcfg)

    selected = _select_languages(presult, languages)
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as e:
        from .errors import FileError
        raise FileError(f"无法创建输出目录 {output_dir}: {e}")

    # batch 模式：跨语言单任务编排（仅 minimax）
    mode = str(pcfg.get("tts_mode") or "batch").lower()
    if mode == "batch" and pname == "minimax":
        return _run_batch(presult.warnings, selected, pcfg, output_dir, emit, token,
                          submit_only, task_store)

    reports: List[LanguageReport] = []
    total = len(selected)
    for i, pack in enumerate(selected, 1):
        token.throw_if_cancelled()
        emit(StageEvent("parse", f"[{i}/{total}] {pack.language}: {len(pack.entries)} 条", i, total))
        pres = provider.synthesize_batch(
            pack.entries,
            pcfg,
            emit,
            token,
            # 边转边落盘：每条合成成功立即写入 <输出目录>/<语言>/<语音ID>.mp3
            on_entry=lambda entry, data, _lang=pack.language: packager.write_single(
                _lang, entry.voice_id, data, output_dir),
        )
        rep = packager.write_language_pack(
            pack.language,
            pres.audio_by_id,
            [e.voice_id for e in pack.entries],
            output_dir,
        )
        rep.warnings += pres.warnings
        _emit_package(emit, rep, i, total)
        reports.append(rep)
    emit(StageEvent("done", "全部完成", total, total))
    return JobReport(list(presult.warnings), reports)


def run_single(
    input_path: str,
    language: str,
    voice_id: str,
    parser_name: Optional[str] = "auto",
    provider_name: Optional[str] = None,
    config: Optional[dict] = None,
    output_dir: str = "语音包",
    on_event: Optional[Callable[[StageEvent], None]] = None,
    cancel_token: Optional[CancelToken] = None,
    parsers_dir: Optional[str] = None,
) -> SingleResult:
    """单条语音转换：解析目录 -> 选语言 -> 选语音ID -> 合成 -> 落盘 mp3。

    合成统一走供应商中间层接口 provider.synthesize_one（兼容所有供应商）：
    MiniMax 覆盖为同步接口 /v1/t2a_v2（一次请求直接返回音频，无任务无轮询）；
    未覆盖的供应商由基类默认实现委托其 synthesize_batch([entry])。
    空文本不调 API，直接抛 ParseError。
    """
    emit = on_event or _noop
    token = cancel_token or CancelToken()
    parsers.load_parsers(parsers_dir or paths.default_parsers_dir())
    providers.load_providers()

    if parser_name in (None, "auto"):
        parser = parsers.detect_best(input_path)
    else:
        parser = parsers.get_parser(parser_name)
    presult = parser.parse(input_path)
    emit(StageEvent("parse", f"解析完成（{parser.name}）: {len(presult.languages)} 种语言"))

    by_lang = {p.language: p for p in presult.languages}
    pack = by_lang.get(language)
    if pack is None:
        raise ParseError(f"未找到语言: {language}，可用: {sorted(by_lang)}")
    by_id = {e.voice_id: e for e in pack.entries}
    entry = by_id.get(voice_id)
    if entry is None:
        raise ParseError(f"语言「{language}」中未找到语音ID: {voice_id}（共 {len(by_id)} 条）")
    if entry.is_empty:
        raise ParseError(f"{language} / {voice_id} 为空文本，无法合成")

    pname = provider_name or (config or {}).get("provider", "minimax")
    provider = providers.get_provider(pname)
    pcfg = config_mod.get_provider_config(config or {}, pname)
    provider.validate_config(pcfg)
    token.throw_if_cancelled()

    emit(StageEvent("create", f"合成 {language}/{voice_id}（{len(entry.text)} 字符）"))
    data = provider.synthesize_one(entry, pcfg, token)

    token.throw_if_cancelled()
    path = packager.write_single(language, voice_id, data, output_dir)
    emit(StageEvent("done", f"已生成 {path}（{len(data)} 字节）", 1, 1))
    return SingleResult(language, voice_id, entry.text, path, len(data))


def _emit_package(emit, rep: LanguageReport, i: int, total: int) -> None:
    """发出 package 阶段事件，汇报单语言成功/缺失情况。"""
    msg = f"{rep.language}: 成功 {len(rep.success_ids)}，缺失 {len(rep.missing_ids)}"
    if rep.missing_ids:
        msg += f"，缺失ID: {', '.join(rep.missing_ids)}"
    if rep.tar_path is None:
        msg += "（全部缺失，未生成语音包）"
    emit(StageEvent("package", msg, i, total))


def _run_batch(parse_warnings, selected, pcfg: dict, output_dir: str, emit,
               token: CancelToken, submit_only: bool,
               task_store: Optional[tasks_mod.TaskStore]) -> JobReport:
    """batch 模式：全部语言合并 1 个 zip、1 次任务（d<码><语音ID>.txt 命名 + entry_map 映射）。"""
    languages = [p.language for p in selected]
    entry_map = {}
    entries = []          # [{"dir","voice_id","text"}]
    empty_missing = {}    # 语言 -> [空文本语音ID]
    for li, pack in enumerate(selected, 1):
        code = f"d{li:04d}"
        entry_map[code] = pack.language
        for e in pack.entries:
            if e.is_empty:
                empty_missing.setdefault(pack.language, []).append(e.voice_id)
                continue
            entries.append({"dir": code, "voice_id": e.voice_id, "text": e.text})

    if not entries:
        reports = [LanguageReport(lang, [], empty_missing.get(lang, []), [],
                                  ["全部条目为空文本"], None, None) for lang in languages]
        emit(StageEvent("done", "全部条目为空文本，未提交任务"))
        return JobReport(list(parse_warnings), reports)

    token.throw_if_cancelled()
    zip_entries = tasks_mod.build_zip_entries(entries)
    emit(StageEvent("zip", f"{len(languages)} 种语言 {len(entries)} 条已打包（单任务）"))
    tid, upload_fid, usage = minimax.submit_batch(zip_entries, pcfg)
    emit(StageEvent("create", f"任务已创建 task_id={tid}（{len(entries)} 条，用量 {usage} 字符）"))

    if submit_only:
        if task_store is None:
            raise ConfigError("submit_only 模式需要 task_store")
        now = time.time()
        rec = tasks_mod.TaskRecord(
            task_id=tid, provider="minimax", languages=languages, entry_map=entry_map,
            entries=entries, entry_count=len(entries), created_at=now, updated_at=now,
            output_dir=output_dir, upload_file_id=upload_fid, usage_characters=usage)
        task_store.upsert(rec)
        for lang in languages:
            if empty_missing.get(lang):
                emit(StageEvent("package",
                                f"{lang}: 空文本跳过 {len(empty_missing[lang])} 条: "
                                f"{', '.join(empty_missing[lang])}"))
        emit(StageEvent("done", f"任务已提交（{tid}），请到「任务中心」刷新/下载"))
        reports = [LanguageReport(lang, [], empty_missing.get(lang, []), [],
                                  [f"任务已提交 task_id={tid}"], None, None)
                   for lang in languages]
        return JobReport(list(parse_warnings), reports, submitted_tasks=[tid])

    # 阻塞路径（CLI）：轮询 -> 下载 -> 分语言解包落盘
    out_fid = minimax._poll(tid, pcfg, token, on_event=emit)
    blob = minimax._download_tar(out_fid, pcfg)
    emit(StageEvent("download", f"结果包已下载 {len(blob)} 字节"))
    audio_by_code, extract_warnings = minimax.extract_named_mp3s(blob)

    reports: List[LanguageReport] = []
    total = len(selected)
    for i, pack in enumerate(selected, 1):
        code = f"d{i:04d}"
        audio_by_id = {}
        for e in pack.entries:
            if e.is_empty:
                continue
            data = audio_by_code.get(f"{code}{e.voice_id}")
            if data:
                audio_by_id[e.voice_id] = data
                packager.write_single(pack.language, e.voice_id, data, output_dir)  # 逐条落盘
        rep = packager.write_language_pack(
            pack.language, audio_by_id, [e.voice_id for e in pack.entries], output_dir)
        rep.warnings += extract_warnings
        _emit_package(emit, rep, i, total)
        reports.append(rep)
    emit(StageEvent("done", "全部完成", total, total))
    return JobReport(list(parse_warnings), reports)
