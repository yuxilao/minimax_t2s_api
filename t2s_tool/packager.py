from __future__ import annotations

import os
import re
import tarfile
from typing import Dict, List

from .errors import FileError
from .models import LanguageReport

_ILLEGAL = re.compile(r'[<>:"/\\|?*]')


def sanitize_name(name) -> str:
    """跨平台目录/文件名清洗：非法字符替换为 _。"""
    cleaned = _ILLEGAL.sub("_", str(name)).strip()
    return cleaned or "未命名"


def write_single(language: str, voice_id: str, data: bytes, output_dir: str) -> str:
    """单条音频立即落盘：<output_dir>/<语言>/<voice_id>.mp3（边转边写用）。"""
    lang_dir = os.path.join(output_dir, sanitize_name(language))
    try:
        os.makedirs(lang_dir, exist_ok=True)
        path = os.path.join(lang_dir, f"{voice_id}.mp3")
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        raise FileError(f"无法写入文件 {path}: {e}")
    return path


def write_language_pack(
    language: str,
    audio_by_id: Dict[str, bytes],
    expected_ids: List[str],
    output_dir: str,
) -> LanguageReport:
    """把某语言的音频按语音ID写入 <output_dir>/<语言>/<vid>.mp3，并打 <语言>.tar（tar内顶层扁平 <vid>.mp3）。

    全部缺失（无一条成功）时不创建空目录、不打空 tar，report 的 output_dir/tar_path 为 None。
    """
    lang = sanitize_name(language)
    lang_dir = os.path.join(output_dir, lang)
    success: List[str] = []
    missing: List[str] = []
    expected_set = set(expected_ids)
    for vid in expected_ids:
        if audio_by_id.get(vid):
            success.append(vid)
        else:
            missing.append(vid)
    unknown = sorted(k for k in audio_by_id if k not in expected_set)
    if not success:
        return LanguageReport(language, success, missing, unknown, [], None, None)
    try:
        os.makedirs(lang_dir, exist_ok=True)
    except OSError as e:
        raise FileError(f"无法创建输出目录 {lang_dir}: {e}")
    for vid in success:
        path = os.path.join(lang_dir, f"{vid}.mp3")
        try:
            with open(path, "wb") as f:
                f.write(audio_by_id[vid])
        except OSError as e:
            raise FileError(f"无法写入文件 {path}: {e}")
    tar_path = os.path.join(output_dir, f"{lang}.tar")
    try:
        with tarfile.open(tar_path, "w") as tar:
            for vid in success:
                tar.add(os.path.join(lang_dir, f"{vid}.mp3"), arcname=f"{vid}.mp3")
    except (OSError, tarfile.TarError) as e:
        raise FileError(f"无法创建语音包 {tar_path}: {e}")
    return LanguageReport(language, success, missing, unknown, [], lang_dir, tar_path)
