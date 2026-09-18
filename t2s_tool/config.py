from __future__ import annotations

import copy
import json
import os
from typing import Dict

from .errors import ConfigError

# 单个供应商配置块的默认值（MiniMax 参数与项目既有 config.json 对齐）
PROVIDER_DEFAULTS: Dict = {
    "api_key": "",
    "base_url": "https://api.minimaxi.com",
    "model": "speech-2.8-turbo",
    "voice_setting": {"voice_id": "male-qn-qingse", "speed": 1.0, "vol": 1.0, "pitch": 0},
    "audio_setting": {"audio_sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 2},
    "poll_interval": 10,     # 秒；测试可注入 0
    "poll_timeout": 1200,    # 秒（20 分钟）；batch 整批任务服务端处理可能数分钟
    "concurrency": 5,        # 并发数（仅 sync/async 逐条模式生效）
    "tts_mode": "batch",     # batch=全部语言合并1个zip单任务(默认)；sync=逐条同步(快)；async=逐条异步
    "sync_max_chars": 9000,  # sync 模式超过该字符数的条目自动回退异步链路
    "request_timeout": 30,
    "upload_timeout": 120,   # batch 模式 zip 上传超时
    "download_timeout": 60,
}


def migrate_config(raw: dict) -> dict:
    """把任意已支持的历史结构迁移为 {"provider": str, "providers": {name: block}}。"""
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象")
    if isinstance(raw.get("providers"), dict):
        cfg = copy.deepcopy(raw)
        cfg.setdefault("provider", "minimax")
        for name, block in list(cfg["providers"].items()):
            merged = copy.deepcopy(PROVIDER_DEFAULTS)
            if isinstance(block, dict):
                merged.update(block)
            cfg["providers"][name] = merged
        return cfg
    # 旧版扁平结构（顶层 api_key/model/voice_setting/audio_setting）自动迁移为 minimax 块
    merged = copy.deepcopy(PROVIDER_DEFAULTS)
    for key in PROVIDER_DEFAULTS:
        if key in raw:
            merged[key] = raw[key]
    return {"provider": raw.get("provider", "minimax"), "providers": {"minimax": merged}}


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置文件 JSON 格式错误: {e}")
    return migrate_config(raw)


def get_provider_config(config: dict, name: str) -> dict:
    """取指定供应商配置块；未知供应商返回默认值副本；minimax 缺 api_key 时报错。"""
    block = (config.get("providers") or {}).get(name)
    if not isinstance(block, dict):
        block = copy.deepcopy(PROVIDER_DEFAULTS)
    if name == "minimax" and not block.get("api_key"):
        raise ConfigError("MiniMax 配置缺少 api_key（请在 config.json 的 providers.minimax.api_key 填写）")
    return block


def save_config(config: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
