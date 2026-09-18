from __future__ import annotations

import os

import pytest

from t2s_tool.errors import FileError, ParseError
from t2s_tool.parsers import (
    detect_best,
    get_parser,
    list_parsers,
    load_parsers,
    register_parser,
)
from t2s_tool.parsers import placeholder, xlsx_9lang  # noqa: F401  (触发注册)
from t2s_tool.parsers.base import ParserBase

REAL_SAMPLE = "/home/beef/Downloads/9国语种+译文-中性语音列表+20230905.xlsx"


def _parser():
    return get_parser("xlsx_9lang")


def _packs_by_lang(result):
    return {p.language: p for p in result.languages}


def test_parse_normal_ordering_and_cleaning(make_xlsx):
    path = make_xlsx(
        ("语音ID", "中文", "英文"),
        [
            ("Q003", "三", "three"),
            ("Q001", " 一\n横 ", "one"),
            ("Q002", "二\r\n二", "two"),
        ],
    )
    result = _parser().parse(path)
    assert result.parser_name == "xlsx_9lang"
    assert result.source_path == path
    packs = _packs_by_lang(result)
    assert list(packs.keys()) == ["中文", "英文"]
    ids = [e.voice_id for e in packs["中文"].entries]
    assert ids == ["Q001", "Q002", "Q003"]  # 按 ID 数字升序
    texts = [e.text for e in packs["中文"].entries]
    assert texts == ["一 横", "二 二", "三"]  # \n→空格、去\r、strip
    assert packs["英文"].entries[0].text == "one"


def test_language_headers_drive_packs(make_xlsx):
    path = make_xlsx(
        ("语音ID", "中文", "英文", "乌克兰语"),
        [("Q001", "一", "one", "один")],
    )
    result = _parser().parse(path)
    assert len(result.languages) == 3
    assert {p.language for p in result.languages} == {"中文", "英文", "乌克兰语"}
    pack_uk = _packs_by_lang(result)["乌克兰语"]
    assert pack_uk.entries[0].text == "один"


def test_skip_rows_with_empty_id(make_xlsx):
    path = make_xlsx(
        ("语音ID", "中文", "英文"),
        [
            ("Q001", "一", "one"),
            ("", "幽灵", "ghost"),
            (None, None, None),
            ("Q002", "二", "two"),
        ],
    )
    result = _parser().parse(path)
    entries = result.languages[0].entries
    assert [e.voice_id for e in entries] == ["Q001", "Q002"]
    assert result.warnings == []  # ID 为空的行静默跳过


def test_illegal_id_with_content_warns(make_xlsx):
    path = make_xlsx(
        ("语音ID", "中文", "英文"),
        [("Q001", "一", "one"), ("BADID", "非法", "bad")],
    )
    result = _parser().parse(path)
    assert [e.voice_id for e in result.languages[0].entries] == ["Q001"]
    assert any("非法" in w for w in result.warnings)


def test_illegal_id_without_content_no_warning(make_xlsx):
    path = make_xlsx(
        ("语音ID", "中文", "英文"),
        [("Q001", "一", "one"), ("BADID", "", "   ", None)],
    )
    result = _parser().parse(path)
    assert [e.voice_id for e in result.languages[0].entries] == ["Q001"]
    assert result.warnings == []


def test_duplicate_id_dedup_with_warning(make_xlsx):
    path = make_xlsx(
        ("语音ID", "中文", "英文"),
        [("Q001", "一", "one"), ("Q001", "重复", "dup"), ("Q002", "二", "two")],
    )
    result = _parser().parse(path)
    entries = result.languages[0].entries
    assert [e.voice_id for e in entries] == ["Q001", "Q002"]
    assert entries[0].text == "一"  # 保留首条
    assert any("重复" in w for w in result.warnings)


def test_empty_text_cell_marks_entry_empty(make_xlsx):
    path = make_xlsx(
        ("语音ID", "中文", "英文"),
        [("Q001", None, "one"), ("Q002", "二", "two")],
    )
    result = _parser().parse(path)
    packs = _packs_by_lang(result)
    q1 = packs["中文"].entries[0]
    assert q1.voice_id == "Q001"
    assert q1.text == ""
    assert q1.is_empty
    assert any("为空文本" in w for w in result.warnings)
    assert not packs["英文"].entries[0].is_empty


def test_parse_no_language_columns_raises(make_xlsx):
    path = make_xlsx(
        ("AP音效名称", "场景详细描述"),
        [("name", "desc")],
        name="nolang.xlsx",
    )
    with pytest.raises(ParseError):
        _parser().parse(path)


def test_parse_empty_first_row_raises(make_xlsx):
    path = make_xlsx((None, None, None), [("Q001", "a", "b")], name="emptyhdr.xlsx")
    with pytest.raises(ParseError):
        _parser().parse(path)


def test_parse_missing_file_raises():
    with pytest.raises(FileError):
        _parser().parse("/nonexistent/dir/list.xlsx")


def test_detect_real_xlsx_scores_80(make_xlsx):
    path = make_xlsx(("语音ID", "中文", "英文"), [("Q001", "一", "one")])
    assert _parser().detect(path) == 80


def test_detect_txt_scores_zero(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text("Q001\t一\n", encoding="utf-8")
    assert _parser().detect(str(p)) == 0


def test_detect_missing_xlsx_scores_zero(tmp_path):
    assert _parser().detect(str(tmp_path / "nope.xlsx")) == 0


def test_detect_corrupt_xlsx_scores_zero(tmp_path):
    p = tmp_path / "corrupt.xlsx"
    p.write_bytes(b"this is not an xlsx file at all")
    assert _parser().detect(str(p)) == 0


def test_detect_best_picks_xlsx_parser(make_xlsx):
    path = make_xlsx(("语音ID", "中文", "英文"), [("Q001", "一", "one")])
    assert detect_best(path).name == "xlsx_9lang"


def test_detect_best_rejects_txt(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text("Q001\t一\n", encoding="utf-8")
    with pytest.raises(ParseError):
        detect_best(str(p))


def test_registry_duplicate_name_raises():
    with pytest.raises(ValueError):
        @register_parser
        class Dup(xlsx_9lang.Xlsx9LangParser):
            name = "xlsx_9lang"

    with pytest.raises(ValueError):
        @register_parser
        class BadName(ParserBase):
            name = "base"


def test_get_parser_unknown_raises():
    with pytest.raises(ParseError):
        get_parser("no-such-parser")


def test_list_parsers_contains_builtins():
    names = {p.name for p in list_parsers()}
    assert {"xlsx_9lang", "placeholder"} <= names


def test_load_parsers_custom_dir(tmp_path):
    custom = tmp_path / "parsers_custom"
    custom.mkdir()
    src = (
        "from __future__ import annotations\n"
        "from t2s_tool.parsers import register_parser\n"
        "from t2s_tool.parsers.base import ParserBase\n"
        "@register_parser\n"
        "class MyP(ParserBase):\n"
        "    name = 'myp'\n"
        "    display_name = 'myp'\n"
        "    def detect(self, path):\n"
        "        return 0\n"
        "    def parse(self, path):\n"
        "        raise NotImplementedError\n"
    )
    (custom / "myp.py").write_text(src, encoding="utf-8")
    load_parsers(str(custom))
    p = get_parser("myp")
    assert p.name == "myp"
    assert p.detect("whatever") == 0
    with pytest.raises(NotImplementedError):
        p.parse("whatever")


def test_placeholder_detect_zero():
    assert get_parser("placeholder").detect("anything.xlsx") == 0


def test_placeholder_parse_raises():
    with pytest.raises(ParseError):
        get_parser("placeholder").parse("anything.xlsx")


@pytest.mark.skipif(not os.path.exists(REAL_SAMPLE), reason="真实样例文件不存在（环境依赖）")
def test_real_sample_file():
    result = _parser().parse(REAL_SAMPLE)
    assert len(result.languages) == 10
    names = [p.language for p in result.languages]
    assert names[0] == "中文语音内容"
    assert names[-1] == "土耳其语"
    for pack in result.languages:
        assert len(pack.entries) == 98
        assert pack.entries[0].voice_id == "Q001"
        assert pack.entries[-1].voice_id == "Q258"
        ids = [e.voice_id for e in pack.entries]
        assert ids == sorted(ids, key=lambda v: int(v[1:]))
