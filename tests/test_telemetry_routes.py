"""Telemetry routes (ADR 0023 phase 3 extraction) — registrar wires the
read-only /api/telemetry/* surface and degrades safely when the store is off."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.telemetry_routes import register_telemetry_routes


def _client(monkeypatch, store):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "telemetry_store", store, raising=False)
    app = FastAPI()
    register_telemetry_routes(app)
    return TestClient(app)


def test_routes_disabled_when_store_off(monkeypatch):
    c = _client(monkeypatch, None)
    assert c.get("/api/telemetry/summary").json() == {"enabled": False, "summary": None}
    assert c.get("/api/telemetry/recent").json() == {"enabled": False, "turns": []}
    assert c.get("/api/telemetry/insights").json() == {"enabled": False, "insights": None}


def test_summary_and_recent_delegate_to_store(monkeypatch):
    class _Store:
        def summary(self, since_iso=None):
            return {"turns": 3, "since": since_iso}

        def recent(self, limit=50):
            return [{"task_id": "t1"}][:limit]

    c = _client(monkeypatch, _Store())
    body = c.get("/api/telemetry/summary?since=2026-01-01").json()
    assert body == {"enabled": True, "summary": {"turns": 3, "since": "2026-01-01"}}
    recent = c.get("/api/telemetry/recent?limit=1").json()
    assert recent == {"enabled": True, "turns": [{"task_id": "t1"}]}


def test_recent_limit_is_clamped(monkeypatch):
    seen = {}

    class _Store:
        def recent(self, limit=50):
            seen["limit"] = limit
            return []

    c = _client(monkeypatch, _Store())
    c.get("/api/telemetry/recent?limit=99999")
    assert seen["limit"] == 500  # clamped to the 500 ceiling
    c.get("/api/telemetry/recent?limit=0")
    assert seen["limit"] == 1  # clamped to the floor
