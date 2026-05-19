"""
Tests for routers/replay.py — stats aggregation, log-correction, atomicity.
Heavy sidecar deps are stubbed in conftest.py.
"""
import json
import os
import sys
import types
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.replay import router, MAX_CORRECTION_CHARS


def _make_client(data_dir: str) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    mock_main = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": mock_main}):
        return TestClient(app)


def _write_log(data_dir: str, entries: list[dict]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "passive_log.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _read_log(data_dir: str) -> list[dict]:
    path = os.path.join(data_dir, "passive_log.jsonl")
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ── /replay/stats ─────────────────────────────────────────────────────────────

def test_stats_no_log(tmp_path):
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.get("/replay/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_count"] == 0
    assert data["correction_count"] == 0
    assert data["avg_final_pass_ms"] is None
    assert data["avg_llm_ms"] is None


def test_stats_counts_entries(tmp_path):
    entries = [
        {"raw_asr": "hello", "user_edited": "Hello.", "corrected": True, "final_pass_ms": 4000},
        {"raw_asr": "world", "user_edited": "World.", "corrected": True, "final_pass_ms": 3000, "llm_ms": 200},
        {"raw_asr": "test", "final_pass_ms": 5000},
    ]
    _write_log(str(tmp_path), entries)
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.get("/replay/stats")
    data = resp.json()
    assert data["session_count"] == 3
    assert data["correction_count"] == 2
    assert data["avg_final_pass_ms"] == 4000  # (4000+3000+5000)//3
    assert data["avg_llm_ms"] == 200


def test_stats_skips_malformed_lines(tmp_path):
    log_path = os.path.join(str(tmp_path), "passive_log.jsonl")
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(log_path, "w") as f:
        f.write('{"raw_asr": "ok"}\n')
        f.write("not valid json\n")
        f.write('{"raw_asr": "also ok"}\n')
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.get("/replay/stats")
    assert resp.json()["session_count"] == 2


# ── /replay/log-correction ────────────────────────────────────────────────────

def test_log_correction_updates_last_entry(tmp_path):
    entries = [
        {"raw_asr": "first entry", "user_edited": ""},
        {"raw_asr": "second entry", "user_edited": ""},
    ]
    _write_log(str(tmp_path), entries)
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post("/replay/log-correction", json={"user_corrected_text": "Second Entry."})
    assert resp.status_code == 200
    lines = _read_log(str(tmp_path))
    assert lines[0]["raw_asr"] == "first entry"
    assert lines[1]["user_edited"] == "Second Entry."
    assert lines[1]["corrected"] is True
    assert "corrected_at" in lines[1]


def test_log_correction_sets_corrected_at_timestamp(tmp_path):
    _write_log(str(tmp_path), [{"raw_asr": "hello"}])
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        c.post("/replay/log-correction", json={"user_corrected_text": "Hello."})
    lines = _read_log(str(tmp_path))
    assert lines[0].get("corrected_at") is not None


def test_log_correction_no_log_returns_404(tmp_path):
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post("/replay/log-correction", json={"user_corrected_text": "text"})
    assert resp.status_code == 404


def test_log_correction_empty_log_returns_404(tmp_path):
    log_path = os.path.join(str(tmp_path), "passive_log.jsonl")
    os.makedirs(str(tmp_path), exist_ok=True)
    open(log_path, "w").close()
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post("/replay/log-correction", json={"user_corrected_text": "text"})
    assert resp.status_code == 404


def test_log_correction_too_long_returns_400(tmp_path):
    _write_log(str(tmp_path), [{"raw_asr": "hello"}])
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post(
            "/replay/log-correction",
            json={"user_corrected_text": "x" * (MAX_CORRECTION_CHARS + 1)},
        )
    assert resp.status_code == 400


def test_log_correction_atomic_write(tmp_path):
    """Verify original file is intact if temp write path exists post-correction."""
    entries = [{"raw_asr": "hi"}]
    _write_log(str(tmp_path), entries)
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        c.post("/replay/log-correction", json={"user_corrected_text": "Hi."})
    # No .tmp residue after successful write
    tmp_files = [f for f in os.listdir(str(tmp_path)) if f.endswith(".tmp")]
    assert tmp_files == []
    # File still valid JSON lines
    lines = _read_log(str(tmp_path))
    assert len(lines) == 1
