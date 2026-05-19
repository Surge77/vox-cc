"""
Tests for routers/snippets.py — CRUD, validation, size limits.
Heavy sidecar deps are stubbed in conftest.py.
"""
import json
import sys
import types
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.snippets import router, MAX_EXPANSION_CHARS, MAX_SNIPPET_COUNT


def _make_client(data_dir: str) -> TestClient:
    """Return a TestClient whose requests see data_dir as the sidecar DATA_DIR."""
    app = FastAPI()
    app.include_router(router)
    mock_main = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": mock_main}):
        return TestClient(app)


@pytest.fixture
def client(tmp_path):
    return _make_client(str(tmp_path)), str(tmp_path)


# ── list ─────────────────────────────────────────────────────────────────────

def test_list_empty(client):
    c, data_dir = client
    with patch.dict(sys.modules, {"main": types.SimpleNamespace(DATA_DIR=data_dir)}):
        resp = c.get("/snippets")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_list_after_add(client):
    c, data_dir = client
    m = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": m}):
        c.post("/snippets", json={"trigger": "brb", "expansion": "be right back"})
        resp = c.get("/snippets")
    assert resp.json() == {"brb": "be right back"}


# ── add ──────────────────────────────────────────────────────────────────────

def test_add_basic(client):
    c, data_dir = client
    m = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post("/snippets", json={"trigger": "omw", "expansion": "on my way"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_add_strips_trigger_whitespace(client):
    c, data_dir = client
    m = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": m}):
        c.post("/snippets", json={"trigger": "  hi  ", "expansion": "hello"})
        resp = c.get("/snippets")
    data = resp.json()
    assert "hi" in data
    assert "  hi  " not in data


def test_add_empty_trigger_rejected(client):
    c, data_dir = client
    m = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post("/snippets", json={"trigger": "   ", "expansion": "text"})
    assert resp.status_code == 400


def test_add_expansion_too_long(client):
    c, data_dir = client
    m = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post(
            "/snippets",
            json={"trigger": "x", "expansion": "a" * (MAX_EXPANSION_CHARS + 1)},
        )
    assert resp.status_code == 400
    assert "character limit" in resp.json()["detail"]


def test_add_at_limit_allowed(client):
    c, data_dir = client
    m = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post(
            "/snippets",
            json={"trigger": "x", "expansion": "a" * MAX_EXPANSION_CHARS},
        )
    assert resp.status_code == 200


def test_add_count_limit(tmp_path):
    """Adding beyond MAX_SNIPPET_COUNT returns 400."""
    import json as _json
    snippets_file = tmp_path / "snippets.json"
    # Pre-fill with MAX_SNIPPET_COUNT entries
    existing = {f"t{i}": f"expansion {i}" for i in range(MAX_SNIPPET_COUNT)}
    snippets_file.write_text(_json.dumps(existing))
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post("/snippets", json={"trigger": "new_one", "expansion": "text"})
    assert resp.status_code == 400
    assert "limit" in resp.json()["detail"]


def test_update_existing_does_not_count_as_new(tmp_path):
    """Updating an existing key never hits the count limit."""
    import json as _json
    snippets_file = tmp_path / "snippets.json"
    existing = {f"t{i}": f"expansion {i}" for i in range(MAX_SNIPPET_COUNT)}
    existing["update_me"] = "old"
    snippets_file.write_text(_json.dumps(existing))
    c = _make_client(str(tmp_path))
    m = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    with patch.dict(sys.modules, {"main": m}):
        resp = c.post("/snippets", json={"trigger": "update_me", "expansion": "new"})
    assert resp.status_code == 200


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_existing(client):
    c, data_dir = client
    m = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": m}):
        c.post("/snippets", json={"trigger": "bye", "expansion": "goodbye"})
        resp = c.delete("/snippets/bye")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_missing_returns_404(client):
    c, data_dir = client
    m = types.SimpleNamespace(DATA_DIR=data_dir)
    with patch.dict(sys.modules, {"main": m}):
        resp = c.delete("/snippets/nonexistent")
    assert resp.status_code == 404
