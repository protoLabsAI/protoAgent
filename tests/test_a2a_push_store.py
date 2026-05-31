"""Tests for the durable A2A push-config store (ADR 0003 / A2A spec)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from a2a_push_store import A2APushStore


def _store(tmp_path):
    return A2APushStore(str(tmp_path / "a2a-push.db"))


def test_set_get_roundtrip(tmp_path):
    s = _store(tmp_path)
    s.set("task-1", url="https://example.com/hook", token="sek", config_id="cfg-1")
    row = s.get("task-1")
    assert row["url"] == "https://example.com/hook"
    assert row["token"] == "sek"
    assert row["config_id"] == "cfg-1"


def test_set_upserts(tmp_path):
    s = _store(tmp_path)
    s.set("task-1", url="https://a/hook", token="t1")
    s.set("task-1", url="https://b/hook", token="t2")  # same task → replace
    row = s.get("task-1")
    assert row["url"] == "https://b/hook" and row["token"] == "t2"
    assert len(s.load()) == 1


def test_delete(tmp_path):
    s = _store(tmp_path)
    s.set("task-1", url="https://a/hook")
    s.delete("task-1")
    assert s.get("task-1") is None


def test_get_missing_returns_none(tmp_path):
    assert _store(tmp_path).get("nope") is None


def test_load_sweeps_expired(tmp_path):
    s = A2APushStore(str(tmp_path / "a2a-push.db"), ttl_s=60)
    old = datetime.now(UTC) - timedelta(seconds=120)
    s.set("stale", url="https://a/hook", now=old)
    s.set("fresh", url="https://b/hook")
    survivors = s.load()
    assert "fresh" in survivors and "stale" not in survivors
    assert s.get("stale") is None  # swept from disk


def test_survives_reopen(tmp_path):
    db = str(tmp_path / "a2a-push.db")
    A2APushStore(db).set("task-1", url="https://a/hook", token="sek")
    # A fresh instance (simulating a restart) sees the persisted config.
    reopened = A2APushStore(db)
    assert reopened.get("task-1")["url"] == "https://a/hook"


@pytest.mark.asyncio
async def test_handler_writes_through_to_store(tmp_path):
    """Registering a push config via the JSON-RPC handler persists it."""
    import a2a_handler as h
    from a2a_handler import PushNotificationConfig, _push_store_set, _push_store_delete

    store = _store(tmp_path)
    prior = h._PUSH_STORE[0]
    h._PUSH_STORE[0] = store
    try:
        cfg = PushNotificationConfig(url="https://example.com/hook", token="sek", id="c1")
        _push_store_set("task-9", cfg)
        assert store.get("task-9")["url"] == "https://example.com/hook"
        _push_store_delete("task-9")
        assert store.get("task-9") is None
        # No-op (no raise) when the store is unset.
        h._PUSH_STORE[0] = None
        _push_store_set("task-x", cfg)
    finally:
        h._PUSH_STORE[0] = prior
