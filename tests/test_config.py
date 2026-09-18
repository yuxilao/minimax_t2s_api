from __future__ import annotations

import copy
import json

import pytest

from t2s_tool.config import (
    PROVIDER_DEFAULTS,
    get_provider_config,
    load_config,
    migrate_config,
    save_config,
)
from t2s_tool.errors import ConfigError


def test_migrate_legacy_flat_dict():
    cfg = migrate_config({"api_key": "k", "model": "m"})
    assert cfg["provider"] == "minimax"
    block = cfg["providers"]["minimax"]
    assert block["api_key"] == "k"
    assert block["model"] == "m"
    # 其余键全部落默认值
    for key in PROVIDER_DEFAULTS:
        assert key in block
    assert block["voice_setting"] == PROVIDER_DEFAULTS["voice_setting"]
    assert block["base_url"] == PROVIDER_DEFAULTS["base_url"]


def test_migrate_legacy_preserves_all_default_keys():
    cfg = migrate_config({})
    block = cfg["providers"]["minimax"]
    assert set(block.keys()) == set(PROVIDER_DEFAULTS.keys())


def test_migrate_new_structure_merges_defaults():
    raw = {"provider": "minimax", "providers": {"minimax": {"api_key": "only-key"}}}
    cfg = migrate_config(raw)
    block = cfg["providers"]["minimax"]
    assert block["api_key"] == "only-key"
    assert block["model"] == PROVIDER_DEFAULTS["model"]
    assert block["audio_setting"] == PROVIDER_DEFAULTS["audio_setting"]
    assert block["poll_timeout"] == PROVIDER_DEFAULTS["poll_timeout"]
    # 深拷贝：不污染默认值与入参
    block["audio_setting"]["format"] = "pcm"
    assert PROVIDER_DEFAULTS["audio_setting"]["format"] == "mp3"
    assert raw["providers"]["minimax"] == {"api_key": "only-key"}


def test_migrate_non_dict_raises():
    with pytest.raises(ConfigError):
        migrate_config([1])


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.json"))


def test_load_config_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json !!", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_load_config_top_level_not_dict(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_save_load_roundtrip(tmp_path):
    path = str(tmp_path / "cfg.json")
    original = migrate_config({"api_key": "rt-key", "model": "speech-x"})
    save_config(original, path)
    loaded = load_config(path)
    assert loaded == original
    # 文件确实是合法 JSON 且中文不转义
    raw = json.loads(open(path, encoding="utf-8").read())
    assert raw["providers"]["minimax"]["api_key"] == "rt-key"


def test_get_provider_config_minimax_missing_key():
    cfg = {"provider": "minimax", "providers": {"minimax": {"model": "m"}}}
    with pytest.raises(ConfigError):
        get_provider_config(cfg, "minimax")
    cfg2 = {"provider": "minimax", "providers": {"minimax": {"api_key": ""}}}
    with pytest.raises(ConfigError):
        get_provider_config(cfg2, "minimax")


def test_get_provider_config_fake_without_block_returns_defaults():
    cfg = {"provider": "minimax", "providers": {}}
    block = get_provider_config(cfg, "fake")
    assert block == copy.deepcopy(PROVIDER_DEFAULTS)
    assert block is not PROVIDER_DEFAULTS


def test_get_provider_config_unknown_name_returns_defaults():
    cfg = {"provider": "minimax", "providers": {"minimax": {"api_key": "k"}}}
    block = get_provider_config(cfg, "nonexistent-vendor")
    assert block == PROVIDER_DEFAULTS


def test_get_provider_config_returns_merged_block():
    cfg = migrate_config({"provider": "minimax",
                          "providers": {"minimax": {"api_key": "kk"}}})
    block = get_provider_config(cfg, "minimax")
    assert block["api_key"] == "kk"
    assert block["model"] == PROVIDER_DEFAULTS["model"]
