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
    # `langfuse_trace_url_template` rides along so the console can turn a row's
    # trace_id into a Langfuse deep link; None when Langfuse isn't configured.
    assert recent == {
        "enabled": True,
        "turns": [{"task_id": "t1"}],
        "langfuse_trace_url_template": None,
    }


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


# ---------------------------------------------------------------------------
# Fleet rollup — GET /api/telemetry/fleet (ADR 0006 fleet extension): hub-side
# read-only fan-out over each member's existing summary+insights reads.
# ---------------------------------------------------------------------------


class _FleetHostStore:
    """Host store with one flagged outlier — enough for summary + insights."""

    def summary(self, since_iso=None):
        return {
            "turns": 4,
            "cost_usd": 0.5,
            "success_rate": 0.75,
            "cache_hit_ratio": 0.6,
            "cache_read_input_tokens": 100,
            "by_model": [{"model": "m", "turns": 4}],
        }

    def outliers(self):
        return [
            {
                "task_id": "h1",
                "trace_id": "trace-host",
                "ended_at": "2026-08-01T00:00:00+00:00",
                "cost_usd": 9.0,
                "reasons": ["cost 9 ≥ 5× median 0.1"],
            }
        ]


_WORKER_PAYLOADS = {
    "w1": {
        "summary": {
            "enabled": True,
            "summary": {"turns": 7, "cost_usd": 1.25, "success_rate": 0.9, "cache_hit_ratio": 0.4},
        },
        "insights": {
            "enabled": True,
            "insights": {
                "turns": 7,
                "flagged": [
                    {
                        "task_id": "t9",
                        "trace_id": "trace-9",
                        "ended_at": "2026-08-02T00:00:00+00:00",
                        "duration_ms": 60000,
                        "reasons": ["latency 60000ms ≥ 5× median 900ms"],
                    }
                ],
                "flagged_count": 1,
            },
        },
    },
}


def _fleet_client(monkeypatch, store, roster, member_payloads):
    """Client with a fake roster + fake member fan-out. ``member_payloads`` is
    slug -> {"summary": body, "insights": body}; a missing slug means both GETs
    failed (the member is unreachable). Returns (client, fan-out call log)."""
    import operator_api.telemetry_routes as tr
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "status", lambda: roster)
    calls = []

    async def _fake_fetch(slug, path):
        calls.append((slug, path))
        kind = "summary" if path.endswith("summary") else "insights"
        return (member_payloads.get(slug) or {}).get(kind)

    monkeypatch.setattr(tr, "_fetch_member_json", _fake_fetch)
    return _client(monkeypatch, store), calls


_HOST_REC = {"name": "hub", "label": "hub", "id": "hub-1", "host": True, "running": True}


def test_fleet_rollup_fans_out_and_merges(monkeypatch):
    import operator_api.telemetry_routes as tr

    monkeypatch.setattr(tr, "_TRACE_URL_TEMPLATE_CACHE", "https://lf.example/project/p/traces/{trace_id}")
    roster = [_HOST_REC, {"name": "worker", "label": "Worker", "id": "w1", "running": True}]
    c, calls = _fleet_client(monkeypatch, _FleetHostStore(), roster, _WORKER_PAYLOADS)
    body = c.get("/api/telemetry/fleet").json()

    # Fan-out went over the member's EXISTING per-member reads, by slug (= id);
    # the host is read locally, never proxied to itself.
    assert ("w1", "api/telemetry/summary") in calls
    assert ("w1", "api/telemetry/insights") in calls
    assert all(slug == "w1" for slug, _ in calls)
    assert body["fleet"] is True

    m = body["members"]["w1"]
    assert m["running"] and m["reachable"] and m["telemetry_enabled"]
    assert m["rollup"] == {"turns": 7, "cost_usd": 1.25, "success_rate": 0.9, "cache_hit_ratio": 0.4}
    (flag,) = m["flags"]
    assert flag["member"] == "w1" and flag["reasons"] == ["latency 60000ms ≥ 5× median 900ms"]
    ev = flag["evidence"]
    assert ev["member"] == "w1"
    assert ev["trace_id"] == "trace-9"
    assert ev["trace_url"] == "https://lf.example/project/p/traces/trace-9"
    assert ev["timestamp"] == "2026-08-02T00:00:00+00:00"
    # The evidence carries the full per-turn row (minus the reasons duplicate).
    assert ev["turn"]["task_id"] == "t9" and ev["turn"]["duration_ms"] == 60000
    assert "reasons" not in ev["turn"]

    # The host merges from the local store under the reserved "host" slug.
    h = body["members"]["host"]
    assert h["host"] is True and h["reachable"] is True and h["rollup"]["turns"] == 4
    (hflag,) = h["flags"]
    assert hflag["evidence"]["trace_url"] == "https://lf.example/project/p/traces/trace-host"


def test_fleet_rollup_reports_unreachable_member(monkeypatch):
    # g1 has no payloads — both member GETs fail. The rollup must still 200,
    # report the member unreachable, and keep the live member's merge intact.
    roster = [
        _HOST_REC,
        {"name": "gone", "label": "Gone", "id": "g1", "running": False, "remote": True},
        {"name": "worker", "label": "Worker", "id": "w1", "running": True},
    ]
    c, _ = _fleet_client(monkeypatch, _FleetHostStore(), roster, _WORKER_PAYLOADS)
    res = c.get("/api/telemetry/fleet")
    assert res.status_code == 200
    members = res.json()["members"]
    assert members["g1"] == {
        "name": "gone",
        "label": "Gone",
        "host": False,
        "remote": True,
        "running": False,
        "reachable": False,
        "telemetry_enabled": False,
        "rollup": None,
        "flags": [],
    }
    assert members["w1"]["reachable"] is True


def test_fleet_rollup_member_with_store_off_is_reachable(monkeypatch):
    # A member that RESPONDS with {enabled: false} is reachable — telemetry off
    # is its store's state, not a connectivity failure.
    roster = [_HOST_REC, {"name": "worker", "id": "w1", "running": True}]
    payloads = {
        "w1": {
            "summary": {"enabled": False, "summary": None},
            "insights": {"enabled": False, "insights": None},
        }
    }
    c, _ = _fleet_client(monkeypatch, _FleetHostStore(), roster, payloads)
    m = c.get("/api/telemetry/fleet").json()["members"]["w1"]
    assert m["reachable"] is True
    assert m["telemetry_enabled"] is False and m["rollup"] is None and m["flags"] == []


def test_fleet_rollup_single_box_degrades_to_local_read(monkeypatch):
    # No members: no fan-out at all, and the top level is byte-identical to the
    # existing single-instance reads (same helpers produce both).
    c, calls = _fleet_client(monkeypatch, _FleetHostStore(), [_HOST_REC], {})
    body = c.get("/api/telemetry/fleet").json()
    assert calls == []
    assert body["fleet"] is False
    assert body["enabled"] is True
    assert body["summary"] == c.get("/api/telemetry/summary").json()["summary"]
    assert body["insights"] == c.get("/api/telemetry/insights").json()["insights"]
    assert list(body["members"]) == ["host"]


def test_fleet_rollup_disabled_when_store_off(monkeypatch):
    # Same degrade contract as the rest of the surface: store off → enabled
    # false everywhere, never an error.
    c, _ = _fleet_client(monkeypatch, None, [_HOST_REC], {})
    body = c.get("/api/telemetry/fleet").json()
    assert body["enabled"] is False and body["summary"] is None and body["insights"] is None
    host = body["members"]["host"]
    assert host["telemetry_enabled"] is False and host["rollup"] is None and host["flags"] == []


def test_fleet_rollup_surface_is_get_only(monkeypatch):
    # Read-only by construction: every /api/telemetry route registers GET only,
    # and a write verb on the fleet read is refused — nothing to restart/mutate.
    c, _ = _fleet_client(monkeypatch, None, [_HOST_REC], {})
    for r in c.app.routes:
        if getattr(r, "path", "").startswith("/api/telemetry"):
            assert set(r.methods) <= {"GET", "HEAD"}
    assert c.post("/api/telemetry/fleet").status_code == 405


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
