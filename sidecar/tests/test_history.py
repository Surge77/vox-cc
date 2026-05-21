"""Tests for routers.replay.filter_history — pure-fn list slicing/search."""
from routers.replay import filter_history, HISTORY_MAX_LIMIT


def _entry(raw: str = "", edited: str = "", exe: str = "") -> dict:
    return {"raw_asr": raw, "user_edited": edited, "executable_name": exe}


def test_empty_input():
    assert filter_history([], "", 200) == []


def test_returns_newest_first():
    entries = [
        _entry(raw="first"),
        _entry(raw="second"),
        _entry(raw="third"),
    ]
    out = filter_history(entries, "", 10)
    assert [e["raw_asr"] for e in out] == ["third", "second", "first"]


def test_applies_limit():
    entries = [_entry(raw=str(i)) for i in range(50)]
    out = filter_history(entries, "", 5)
    assert len(out) == 5
    # Newest 5: 49, 48, 47, 46, 45
    assert [e["raw_asr"] for e in out] == ["49", "48", "47", "46", "45"]


def test_limit_clamped_to_max():
    entries = [_entry(raw=str(i)) for i in range(HISTORY_MAX_LIMIT + 50)]
    out = filter_history(entries, "", HISTORY_MAX_LIMIT + 100)
    assert len(out) == HISTORY_MAX_LIMIT


def test_limit_clamped_to_at_least_one():
    entries = [_entry(raw="only")]
    out = filter_history(entries, "", 0)
    assert len(out) == 1


def test_substring_search_in_user_edited():
    entries = [
        _entry(edited="hello world"),
        _entry(edited="goodbye"),
        _entry(edited="HELLO again"),
    ]
    out = filter_history(entries, "hello", 10)
    assert len(out) == 2
    # Newest-first: "HELLO again" then "hello world"
    assert out[0]["user_edited"] == "HELLO again"
    assert out[1]["user_edited"] == "hello world"


def test_substring_search_in_raw_asr():
    entries = [
        _entry(raw="kubernetes deployment"),
        _entry(raw="ordinary text"),
    ]
    out = filter_history(entries, "kubernetes", 10)
    assert len(out) == 1
    assert out[0]["raw_asr"] == "kubernetes deployment"


def test_substring_search_in_executable_name():
    entries = [
        _entry(raw="hi", exe="code"),
        _entry(raw="hello", exe="notepad"),
    ]
    out = filter_history(entries, "code", 10)
    assert len(out) == 1


def test_case_insensitive_search():
    entries = [_entry(edited="Hello World")]
    out = filter_history(entries, "WORLD", 10)
    assert len(out) == 1


def test_empty_query_returns_all():
    entries = [_entry(raw="a"), _entry(raw="b")]
    out = filter_history(entries, "", 10)
    assert len(out) == 2


def test_whitespace_only_query_returns_all():
    entries = [_entry(raw="a"), _entry(raw="b")]
    out = filter_history(entries, "   ", 10)
    assert len(out) == 2


def test_no_match_returns_empty():
    entries = [_entry(raw="hello"), _entry(raw="world")]
    out = filter_history(entries, "kubernetes", 10)
    assert out == []


def test_skips_entries_missing_string_fields():
    entries = [
        {"raw_asr": None},  # malformed entry
        _entry(raw="real entry"),
    ]
    # Should not crash; only "real entry" matches
    out = filter_history(entries, "real", 10)
    assert len(out) == 1
