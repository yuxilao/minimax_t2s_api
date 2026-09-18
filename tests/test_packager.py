from __future__ import annotations

import os
import tarfile

import pytest

from t2s_tool import packager


def _tar_names(tar_path):
    with tarfile.open(tar_path) as tf:
        return sorted(m.name for m in tf.getmembers() if m.isfile())


def test_write_language_pack_normal(tmp_path):
    audio = {"Q001": b"aaa", "Q002": b"bb"}
    expected = ["Q001", "Q002", "Q003"]
    rep = packager.write_language_pack("中文", audio, expected, str(tmp_path))

    assert rep.language == "中文"
    assert rep.success_ids == ["Q001", "Q002"]
    assert rep.missing_ids == ["Q003"]
    assert rep.unknown_ids == []
    assert rep.warnings == []

    assert rep.output_dir == os.path.join(str(tmp_path), "中文")
    assert rep.tar_path == os.path.join(str(tmp_path), "中文.tar")
    # 扁平文件布局 <语言>/<vid>.mp3
    with open(os.path.join(rep.output_dir, "Q001.mp3"), "rb") as f:
        assert f.read() == b"aaa"
    assert os.path.isfile(os.path.join(rep.output_dir, "Q002.mp3"))
    assert not os.path.exists(os.path.join(rep.output_dir, "Q003.mp3"))
    # tar 内成员扁平
    assert _tar_names(rep.tar_path) == ["Q001.mp3", "Q002.mp3"]


@pytest.mark.parametrize("raw,expected", [
    ('欧洲西语/x:y<>"|?*', '欧洲西语_x_y______'),
    ("", "未命名"),
    ("   ", "未命名"),
    (None, "None"),
    ("正常名字", "正常名字"),
    ("a\\b", "a_b"),
])
def test_sanitize_name(raw, expected):
    assert packager.sanitize_name(raw) == expected


def test_unknown_audio_ids_not_packed(tmp_path):
    audio = {"Q001": b"x", "Q009": b"ghost"}
    rep = packager.write_language_pack("英文", audio, ["Q001", "Q002"], str(tmp_path))
    assert rep.success_ids == ["Q001"]
    assert rep.missing_ids == ["Q002"]
    assert rep.unknown_ids == ["Q009"]
    assert not os.path.exists(os.path.join(rep.output_dir, "Q009.mp3"))
    assert _tar_names(rep.tar_path) == ["Q001.mp3"]


def test_empty_success_creates_nothing(tmp_path):
    """全部缺失：不创建空目录、不打空 tar，报告路径为 None。"""
    rep = packager.write_language_pack("法语", {}, ["Q001", "Q002"], str(tmp_path))
    assert rep.success_ids == []
    assert rep.missing_ids == ["Q001", "Q002"]
    assert rep.output_dir is None
    assert rep.tar_path is None
    assert not (tmp_path / "法语").exists()
    assert not (tmp_path / "法语.tar").exists()
