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


def test_export_returns_csv(monkeypatch):
    class _Store:
        def stream_rows(self, since_iso=None):
            yield {
                "task_id": "t1",
                "session_id": "s",
                "model": "m",
                "cost_usd": 0.01,
                "ended_at": "2026-06-07T10:00:00+00:00",
            }
            yield {
                "task_id": "t2",
                "session_id": "s",
                "model": "m",
                "cost_usd": 0.02,
                "ended_at": "2026-06-08T10:00:00+00:00",
            }

    c = _client(monkeypatch, _Store())
    res = c.get("/api/telemetry/export")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "attachment; filename=" in res.headers.get("content-disposition", "")
    body = res.text
    assert body.splitlines()[0].startswith("task_id,")  # header from _COLUMNS
    assert "t1" in body and "t2" in body


def test_export_since_filter(monkeypatch):
    """The since param is forwarded to stream_rows; SQL does the filtering."""
    seen = {}

    class _Store:
        def stream_rows(self, since_iso=None):
            seen["since_iso"] = since_iso
            if since_iso:
                yield {"task_id": "new", "ended_at": "2026-06-09T00:00:00+00:00"}
            else:
                yield {"task_id": "old", "ended_at": "2026-06-01T00:00:00+00:00"}
                yield {"task_id": "new", "ended_at": "2026-06-09T00:00:00+00:00"}

    c = _client(monkeypatch, _Store())
    body = c.get("/api/telemetry/export?since=2026-06-05T00:00:00+00:00").text
    assert "new" in body and "old" not in body
    # Verify since was passed through (URL-decoded + restored to +00:00)
    assert seen["since_iso"] == "2026-06-05T00:00:00+00:00"


def test_export_empty_when_store_off(monkeypatch):
    c = _client(monkeypatch, None)
    res = c.get("/api/telemetry/export")
    assert res.status_code == 200 and res.text.splitlines()[0].startswith("task_id,")


def test_export_is_streaming_response(monkeypatch):
    """The export uses StreamingResponse with text/csv and calls stream_rows
    (not recent)."""
    called = {"stream_rows": False, "recent": False}

    class _Store:
        def stream_rows(self, since_iso=None):
            called["stream_rows"] = True
            yield {"task_id": "t1", "ended_at": "2026-06-10T00:00:00+00:00"}

        def recent(self, limit=50):
            called["recent"] = True
            return []

    c = _client(monkeypatch, _Store())
    res = c.get("/api/telemetry/export")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    # stream_rows was called, not recent
    assert called["stream_rows"]
    assert not called["recent"]
    # Body contains the header and the row
    lines = res.text.strip().splitlines()
    assert lines[0].startswith("task_id,")  # CSV header
    assert "t1" in res.text
