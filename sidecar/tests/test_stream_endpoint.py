"""Tests for the SSE plumbing in /process-text/stream.

The endpoint itself depends on a real LLM, so these only cover the pure-Python
pieces we can isolate: the SSE packing helper, and the heuristic shortcut path.
"""
import json

from routers.text_processing import _sse_pack, is_simple_clean_text


def test_sse_pack_dict_payload():
    out = _sse_pack("delta", {"text": "hello"})
    assert out.startswith("event: delta\n")
    assert "data: {" in out
    assert out.endswith("\n\n")
    # data line must be JSON-parseable
    data_line = [ln for ln in out.split("\n") if ln.startswith("data: ")][0]
    body = data_line[len("data: "):]
    parsed = json.loads(body)
    assert parsed == {"text": "hello"}


def test_sse_pack_string_payload():
    # Strings are passed through verbatim (no JSON encoding)
    out = _sse_pack("done", "ok")
    assert "event: done\n" in out
    assert "data: ok\n" in out


def test_sse_pack_done_envelope():
    out = _sse_pack("done", {"cleaned_text": "Hi.", "error": None})
    parsed = json.loads(out.split("data: ", 1)[1].split("\n", 1)[0])
    assert parsed["cleaned_text"] == "Hi."
    assert parsed["error"] is None


def test_unicode_payload_round_trips():
    out = _sse_pack("delta", {"text": "café — naïve"})
    parsed = json.loads(out.split("data: ", 1)[1].split("\n", 1)[0])
    assert parsed["text"] == "café — naïve"


def test_heuristic_skip_text_is_short_and_clean():
    # Same heuristic gates the streaming early-return branch
    assert is_simple_clean_text("Hello world.")
    assert not is_simple_clean_text("hello world")
