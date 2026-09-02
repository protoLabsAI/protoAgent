"""Telemetry routes (ADR 0023 phase 3 extraction) — registrar wires the
read-only /api/telemetry/* surface and degrades safely when the store is off."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.telemetry_routes import register_telemetry_routes


def _client(monkeypatch, store, tracing_enabled=False):
    import runtime.state as rs
    from observability import tracing

    monkeypatch.setattr(rs.STATE, "telemetry_store", store, raising=False)
    # Pin the tracing flag rather than reading whatever a sibling test file left on the
    # module — the response now reports it (#3017), so it has to be deterministic here.
    monkeypatch.setattr(tracing, "is_enabled", lambda: tracing_enabled)
    app = FastAPI()
    register_telemetry_routes(app)
    return TestClient(app)


def test_routes_disabled_when_store_off(monkeypatch):
    c = _client(monkeypatch, None)
    assert c.get("/api/telemetry/summary").json() == {"enabled": False, "summary": None}
    # tracing_enabled rides along even with the store off — the two are independent
    # switches, and the console needs the answer to explain an empty Trace column.
    assert c.get("/api/telemetry/recent").json() == {
        "enabled": False,
        "turns": [],
        "tracing_enabled": False,
    }
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
        "tracing_enabled": False,
    }


def test_recent_reports_tracing_off_so_an_empty_trace_column_can_say_why(monkeypatch):
    """#3017: with Langfuse off EVERY row's trace_id is blank, and a column of dashes
    reads as "these turns weren't traced" rather than "tracing is disabled". The
    response carries the distinction so the console can state it."""

    class _Store:
        def recent(self, limit=50):
            return [{"task_id": "t1", "trace_id": None}]

    c = _client(monkeypatch, _Store(), tracing_enabled=False)
    assert c.get("/api/telemetry/recent").json()["tracing_enabled"] is False


def test_recent_reports_tracing_on_when_langfuse_is_live(monkeypatch):
    class _Store:
        def recent(self, limit=50):
            return [{"task_id": "t1", "trace_id": "abc"}]

    c = _client(monkeypatch, _Store(), tracing_enabled=True)
    assert c.get("/api/telemetry/recent").json()["tracing_enabled"] is True


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


def test_fleet_rollup_survives_member_that_raises_during_resolution(monkeypatch):
    # #3018: target resolution sat OUTSIDE `_fetch_member_json`'s try, so one
    # member raising there returned HTTP 500 for the whole rollup — every
    # healthy member's data and the host's own numbers lost with it.
    #
    # The raise is driven through the REAL `_target_for_slug` over a REAL
    # malformed record rather than a stubbed side_effect, because the mechanism
    # is easy to get wrong: `_load_state` is *tolerant* of an unreadable file
    # (it logs and returns {}), so what actually reaches this route is a bad
    # RECORD — here a state entry this hub believes is running that carries no
    # `port`, which `_target_for_slug` subscripts straight through.
    import os

    from graph.fleet import proxy, supervisor

    roster = [
        _HOST_REC,
        {"name": "bad", "label": "Bad", "id": "bad", "running": True},
        {"name": "okay", "label": "Okay", "id": "okay", "running": True},
    ]
    monkeypatch.setattr(supervisor, "status", lambda: roster)
    monkeypatch.setattr(supervisor, "_load_state", lambda: {"bad": {"pid": os.getpid()}})
    # No remote lookups: "okay" then resolves cleanly to None (merely
    # unreachable), and the test never reads the developer's real remotes
    # registry — so it can never dial a real member.
    monkeypatch.setattr(supervisor, "remote_for_slug", lambda slug: None)
    monkeypatch.setattr(proxy, "_slug_cache", {})  # 1s memo — never inherit a neighbour's

    # Guard the mechanism itself: if `_target_for_slug` ever stops raising here,
    # the assertions below would pass without exercising the bug at all.
    with pytest.raises(KeyError):
        proxy._target_for_slug("bad")

    res = _client(monkeypatch, _FleetHostStore()).get("/api/telemetry/fleet")
    assert res.status_code == 200
    body = res.json()

    # The member that raised is REPORTED unreachable — not dropped, not fatal —
    # in the same shape any other unreadable member gets.
    assert body["members"]["bad"] == {
        "name": "bad",
        "label": "Bad",
        "host": False,
        "remote": False,
        "running": True,
        "reachable": False,
        "telemetry_enabled": False,
        "rollup": None,
        "flags": [],
    }
    assert body["members"]["okay"]["reachable"] is False
    # ...and the host's own telemetry still renders, which is what the 500 ate.
    assert body["enabled"] is True
    assert body["members"]["host"]["rollup"]["turns"] == 4
    assert body["members"]["host"]["flags"][0]["evidence"]["trace_id"] == "trace-host"


def test_fleet_rollup_survives_service_token_failure(monkeypatch):
    # The other call #3018 moved inside the boundary: a LOCAL member's read
    # mints the fleet service token (ADR 0089 D3) before the GET, and
    # `resolve_service_token` reads-or-creates a file — so it can raise on its
    # own. That was one member's problem escaping as the rollup's.
    from graph.fleet import proxy, service_token, supervisor

    def _boom():
        raise RuntimeError("service-token file unreadable")

    roster = [_HOST_REC, {"name": "bad", "label": "Bad", "id": "bad", "running": True}]
    monkeypatch.setattr(supervisor, "status", lambda: roster)
    # A LOCAL target — no authorization in the extra headers — is exactly the
    # case that mints. Port 9 (discard) is never dialed: the mint raises first.
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://127.0.0.1:9", {}))
    monkeypatch.setattr(service_token, "resolve_service_token", _boom)

    res = _client(monkeypatch, _FleetHostStore()).get("/api/telemetry/fleet")
    assert res.status_code == 200
    body = res.json()
    assert body["members"]["bad"]["reachable"] is False
    assert body["members"]["host"]["rollup"]["turns"] == 4


def test_fleet_rollup_survives_raise_after_the_fetch_boundary(monkeypatch):
    # Belt and braces on the same contract (#3018): the peers gather runs with
    # return_exceptions=True, so a raise anywhere in one member's read — here
    # in the merge step, past `_fetch_member_json` entirely — still degrades to
    # one unreachable row. Silently omitting the member would be its own lie.
    import operator_api.telemetry_routes as tr

    roster = [
        _HOST_REC,
        {"name": "bad", "label": "Bad", "id": "bad", "running": True, "remote": True},
        {"name": "worker", "label": "Worker", "id": "w1", "running": True},
    ]
    real_member_entry = tr._member_entry

    def _boom(rec, slug, summary_payload, insights_payload, template):
        if slug == "bad":
            raise RuntimeError("merge step blew up")
        return real_member_entry(rec, slug, summary_payload, insights_payload, template)

    c, _ = _fleet_client(monkeypatch, _FleetHostStore(), roster, _WORKER_PAYLOADS)
    monkeypatch.setattr(tr, "_member_entry", _boom)

    res = c.get("/api/telemetry/fleet")
    assert res.status_code == 200
    members = res.json()["members"]
    # Present, and shaped exactly like any other member we could not read.
    assert members["bad"] == {
        "name": "bad",
        "label": "Bad",
        "host": False,
        "remote": True,
        "running": True,
        "reachable": False,
        "telemetry_enabled": False,
        "rollup": None,
        "flags": [],
    }
    assert members["w1"]["reachable"] is True and members["w1"]["rollup"]["turns"] == 7
    assert members["host"]["rollup"]["turns"] == 4


def _real_registry_client(monkeypatch, tmp_path, remotes):
    """A rollup client whose roster comes from the REAL fleet registry.

    ``supervisor.status`` is NOT stubbed and neither is target resolution: the
    only input is a raw ``remotes.json`` on disk — the file an operator can (and
    per #3018 did) hand-edit. That is the whole point of these two tests: the
    roster read sits OUTSIDE the rollup's containment boundary, so it has to be
    total on its own, and a stub would prove nothing about that.
    """
    import json

    from graph.fleet import proxy, supervisor
    from graph.workspaces import manager

    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    root = manager.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "remotes.json").write_text(json.dumps(remotes))
    supervisor._probe_cache.clear()
    monkeypatch.setattr(proxy, "_slug_cache", {})  # 1s memo — never inherit a neighbour's
    return _client(monkeypatch, _FleetHostStore())


def test_fleet_rollup_survives_a_malformed_remote_record(tmp_path, monkeypatch):
    # #3018, the half that lives in the ROSTER read rather than the member read:
    # ``status()`` subscripted every remote record while building the roster
    # (``rec["url"]``), and the route's first statement is that read — so a
    # single field missing from one hand-edited record 500'd the entire rollup
    # before any member was ever contacted. Fixed at the source in graph/fleet,
    # because /api/fleet reads the same registry and broke on the same input.
    from graph.fleet import proxy

    c = _real_registry_client(monkeypatch, tmp_path, {"ava-1a2b": {"id": "ava-1a2b", "name": "ava", "label": "Ava"}})

    # Guard the mechanism: a record with no url still genuinely raises during
    # resolution, so the assertions below cannot go quietly vacuous. It reaching
    # that boundary at all is what the roster fix bought.
    with pytest.raises(KeyError):
        proxy._target_for_slug("ava-1a2b")

    res = c.get("/api/telemetry/fleet")
    assert res.status_code == 200
    body = res.json()
    # Reported, in the same shape every other unreadable member gets — the
    # acceptance criterion #3018 states: unreachable, not a failed rollup.
    assert body["members"]["ava-1a2b"] == {
        "name": "ava",
        "label": "Ava",
        "host": False,
        "remote": True,
        "running": False,
        "reachable": False,
        "telemetry_enabled": False,
        "rollup": None,
        "flags": [],
    }
    # ...and the host's own telemetry still renders, which is what the 500 ate.
    assert body["enabled"] is True
    assert body["members"]["host"]["rollup"]["turns"] == 4
    assert body["members"]["host"]["flags"][0]["evidence"]["trace_id"] == "trace-host"


def test_fleet_rollup_survives_a_list_shaped_remotes_file(tmp_path, monkeypatch):
    # The other roster-read input (#3018): remotes.json holding a JSON LIST.
    # It parses, then breaks the first reader — an AttributeError out of
    # ``list_remotes()`` that escaped ``status()`` as a 500. A wrong-shaped top
    # level names no members recoverably, so it degrades to a members-less
    # fleet; what must NOT happen is losing the host's numbers with it.
    c = _real_registry_client(monkeypatch, tmp_path, [{"id": "r1", "url": "http://h:1"}])

    res = c.get("/api/telemetry/fleet")
    assert res.status_code == 200
    body = res.json()
    assert list(body["members"]) == ["host"]
    assert body["enabled"] is True
    assert body["members"]["host"]["rollup"]["turns"] == 4


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


def _lane(model, **over):
    """One `by_model` row as the store emits it (#3342) — the cache operands per lane."""
    return {
        "model": model,
        "turns": 50,
        "input_tokens": 900_000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_hit_ratio": 0.0,
        "p95_context_tokens": 120_000,
        **over,
    }


class _CacheStore:
    """A store whose summary the cache verdict is computed from (#3342).

    The store-wide totals are DERIVED from the lanes rather than set independently, so a
    test can't accidentally describe an agent whose rollup contradicts its own rows.
    """

    def __init__(self, lanes=None, **over):
        lanes = [_lane("gpt-5.6-terra")] if lanes is None else lanes
        self.s = {
            "turns": sum(int(m["turns"]) for m in lanes),
            "p95_context_tokens": max([int(m["p95_context_tokens"]) for m in lanes] or [0]),
            "cache_read_input_tokens": sum(int(m["cache_read_input_tokens"]) for m in lanes),
            "cache_creation_input_tokens": sum(int(m["cache_creation_input_tokens"]) for m in lanes),
            "cache_hit_ratio": 0.0,
            "by_model": lanes,
            **over,
        }

    def summary(self, since_iso=None):
        return self.s

    def outliers(self):
        return []


def _cache(c):
    return c.get("/api/telemetry/insights").json()["insights"]["levers"]["cache"]


def test_insights_report_caching_is_not_engaging(monkeypatch):
    """The whole point of #3342: the middleware knows within three calls that a lane
    bills full input price, but it says so in a log line and a best-effort Activity
    emit — which is how an openai-codex lane went four days unread. The same fact is
    legible from the rows, so it surfaces where cache performance is looked at."""
    c = _client(monkeypatch, _CacheStore())
    cache = _cache(c)
    assert cache["engaging"] is False
    assert cache["model"] == "gpt-5.6-terra"
    assert [m["model"] for m in cache["cold_lanes"]] == ["gpt-5.6-terra"]


def test_insights_report_caching_is_working(monkeypatch):
    c = _client(monkeypatch, _CacheStore([_lane("gpt-5.6-terra", cache_read_input_tokens=900_000)]))
    cache = _cache(c)
    assert cache["engaging"] is True
    assert cache["cold_lanes"] == []


def test_no_cache_verdict_without_enough_evidence(monkeypatch):
    """`None`, not False — the console shows nothing rather than accusing a lane that
    simply hasn't warmed up, or one whose prompts are too small to be cacheable at all
    (every provider has a floor, Anthropic's is ~1024 tokens)."""
    assert _cache(_client(monkeypatch, _CacheStore([_lane("m", turns=3)])))["engaging"] is None
    assert _cache(_client(monkeypatch, _CacheStore([_lane("m", p95_context_tokens=500)])))["engaging"] is None


def test_a_cache_write_alone_counts_as_engaging(monkeypatch):
    """First call of a cold prefix writes and reads nothing. That is caching working,
    not failing — judging on reads alone would flag a healthy lane."""
    c = _client(monkeypatch, _CacheStore([_lane("m", cache_creation_input_tokens=50_000)]))
    assert _cache(c)["engaging"] is True


def test_a_caching_lane_does_not_mask_a_cold_one(monkeypatch):
    """Why the verdict is per-lane at all.

    A store-wide figure is an average over lanes that have nothing to do with each
    other. Measured on protoEngineer's live store: a rollup of 0.511 spans lanes from
    0.5629 down to a lane at 0.0 across 17 turns and 3.4M input tokens. The rollup can
    only ever say "something is fine on average"; it structurally cannot name a lane,
    and one busy healthy lane will always outvote a quiet broken one."""
    c = _client(
        monkeypatch,
        _CacheStore([
            _lane("claude-opus-5", cache_read_input_tokens=4_000_000),
            _lane("openai-codex", turns=38),
        ]),
    )
    cache = _cache(c)
    assert cache["engaging"] is False
    # And it names the guilty lane, not the dominant one.
    assert [m["model"] for m in cache["cold_lanes"]] == ["openai-codex"]
    assert cache["cold_lanes"][0]["turns"] == 38


def test_an_unmeasured_lane_is_not_accused(monkeypatch):
    """An ACP coder leg runs outside the gateway and reports no usage at all (#3015):
    zero input, zero cache. That is unmeasured, not uncached — reading it as "not
    caching" is the false accusation this verdict exists to stop making."""
    c = _client(
        monkeypatch,
        _CacheStore([
            _lane("claude-opus-5", cache_read_input_tokens=4_000_000),
            _lane("acp:coder", input_tokens=0),
        ]),
    )
    cache = _cache(c)
    assert cache["engaging"] is True
    assert cache["cold_lanes"] == []


def test_only_unmeasured_lanes_yields_no_verdict(monkeypatch):
    """An agent that runs nothing but coder legs has no evidence either way."""
    assert _cache(_client(monkeypatch, _CacheStore([_lane("acp:coder", input_tokens=0)])))["engaging"] is None
