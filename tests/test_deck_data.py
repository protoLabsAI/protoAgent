"""deck.data — the deck's backends (#3468): a live hub over a mock transport, and the
offline disk view built from supervisor callables. Every pane degrades on its own."""

from __future__ import annotations

from pathlib import Path

import httpx

from deck import data as deckdata
from deck import hub

ROSTER = [
    {"name": "protoagent", "id": "protoagent", "port": 7870, "pid": 100, "running": True, "host": True, "version": "0.165.0"},
    {"name": "protoEngineer", "id": "protoEngineer-ba4c", "port": 7875, "pid": 15285, "running": True, "version": "0.165.0"},
    {"name": "old", "id": "old-1", "port": 7890, "pid": 77, "running": True, "version": "0.164.0"},
    {"name": "Cindi", "id": "Cindi-9f49", "port": 7880, "pid": None, "running": False, "version": "", "bundle": "cowork-stack"},
    {"name": "ava", "id": "r-ava", "port": None, "pid": None, "running": False, "remote": True, "url": "https://ava.tail:7870"},
]


def _conn(handler) -> hub.Connection:
    transport = httpx.MockTransport(handler)
    client = hub.HubClient("http://127.0.0.1:7870", "tok", transport=transport)
    cand = hub.HubCandidate("http://127.0.0.1:7870", "heartbeat")
    return hub.Connection(client=client, candidate=cand, card={"name": "protoagent"}, roster=list(ROSTER))


def _hub(request: httpx.Request) -> httpx.Response | None:
    p = request.url.path
    if p == "/api/fleet":
        return httpx.Response(200, json={"agents": ROSTER})
    if p == "/api/telemetry/fleet":
        return httpx.Response(
            200,
            json={
                "members": {
                    "host": {"reachable": True, "telemetry_enabled": True, "rollup": {"turns": 3, "cost_usd": 3.1, "success_rate": 1.0, "cache_hit_ratio": 0.5}},
                    "protoEngineer-ba4c": {"reachable": True, "telemetry_enabled": True, "rollup": {"turns": 38, "cost_usd": 12.4, "success_rate": 0.97, "cache_hit_ratio": 0.61}},
                    "old-1": {"reachable": False, "telemetry_enabled": True, "rollup": None},
                    "Cindi-9f49": {"reachable": True, "telemetry_enabled": False, "rollup": None},
                }
            },
        )
    if p == "/api/runtime/status":
        return httpx.Response(200, json={"warnings": ["1 fleet member(s) run a different protoAgent version", {"message": "oauth token stale", "provider": "anthropic"}]})
    return None


def test_slug_and_presence_and_display():
    assert deckdata.slug_of(ROSTER[0]) == "host"
    assert deckdata.slug_of(ROSTER[1]) == "protoEngineer-ba4c"
    assert [deckdata.presence_of(a) for a in ROSTER] == ["host", "online", "online", "stopped", "unreachable"]
    assert deckdata.display_name({"name": "x", "label": "X Label"}) == "X Label"


def test_live_snapshot_uses_the_connect_roster_then_polls_and_folds_extras():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _hub(request) or httpx.Response(404)

    be = deckdata.LiveBackend(_conn(handler))
    snap = be.snapshot()
    assert snap.mode == "live"
    assert snap.label == "live · http://127.0.0.1:7870 · protoagent v0.165.0 · via heartbeat"
    assert [a["name"] for a in snap.roster] == [a["name"] for a in ROSTER]
    assert snap.host_version == "0.165.0"
    assert calls.count("/api/fleet") == 0  # first poll reuses the roster that opened the connection
    assert snap.rollups["protoEngineer-ba4c"].cost_usd == 12.4
    assert snap.rollups["old-1"].reachable is False
    assert snap.rollups["Cindi-9f49"].enabled is False
    assert snap.warnings == ["1 fleet member(s) run a different protoAgent version", "oauth token stale"]
    assert snap.skewed(ROSTER[2]) and not snap.skewed(ROSTER[1]) and not snap.skewed(ROSTER[3])

    snap2 = be.snapshot()
    assert calls.count("/api/fleet") == 1  # the second poll really reads the hub
    assert not snap2.error


def test_live_snapshot_extras_failing_never_fail_the_poll():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/fleet":
            return httpx.Response(200, json={"agents": ROSTER})
        raise httpx.ReadTimeout("slow", request=request)

    be = deckdata.LiveBackend(_conn(handler))
    be.snapshot()  # consumes the connect roster
    snap = be.snapshot()
    assert snap.roster and not snap.error and snap.rollups == {} and snap.warnings == []


def test_live_snapshot_roster_failure_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    be = deckdata.LiveBackend(_conn(handler))
    be.snapshot()
    snap = be.snapshot()
    assert snap.roster == [] and "did not answer" in snap.error


def test_live_detail_reads_through_the_slug_proxy_and_degrades_per_pane():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        paths.append(p)
        if p == "/agents/protoEngineer-ba4c/api/runtime/status":
            return httpx.Response(200, json={"model": {"name": "claude-fable-5-1", "provider": "gateway"}, "identity": {"name": "protoEngineer"}, "version": "0.165.0", "warnings": []})
        if p == "/agents/protoEngineer-ba4c/api/diagnostics/logs":
            assert request.url.params["lines"] == "200"
            return httpx.Response(200, json={"enabled": True, "capacity": 1024, "returned": 1, "lines": [{"ts": "2026-09-12T09:41:02+00:00", "level": "INFO", "logger": "a2a", "message": "task working"}]})
        if p == "/agents/protoEngineer-ba4c/api/diagnostics/sessions":
            return httpx.Response(503, json={"sessions": [], "detail": "task store is not configured on this member"})
        if p == "/agents/old-1/api/diagnostics/sessions":
            return httpx.Response(200, json={"sessions": [{"session_id": "chat-1", "context_id": "chat-1", "latest_task_id": "t1", "latest_task_state": "TASK_STATE_WORKING", "last_activity": "2026-09-12T09:00:00+00:00", "status": "ok", "malformed": []}]})
        return httpx.Response(404)

    be = deckdata.LiveBackend(_conn(handler))
    d = be.detail(ROSTER[1])
    assert d.slug == "protoEngineer-ba4c" and d.name == "protoEngineer"
    assert d.runtime["model"]["name"] == "claude-fable-5-1" and not d.runtime_error
    assert d.logs[0]["message"] == "task working" and not d.logs_error
    assert d.sessions == [] and "503" in d.sessions_error  # one pane down, the others fine
    assert be.console_href(ROSTER[1]) == "http://127.0.0.1:7870/app/agent/protoEngineer-ba4c/"
    assert be.console_href(ROSTER[0]) == "http://127.0.0.1:7870/app/"


def test_live_snapshot_survives_a_malformed_member_rollup_and_non_dict_telemetry():
    """One member's bad telemetry stays local to that member (CodeRabbit Major)."""

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/fleet":
            return httpx.Response(200, json={"agents": ROSTER})
        if p == "/api/telemetry/fleet":
            return httpx.Response(200, json={"members": {"protoEngineer-ba4c": {"reachable": True, "rollup": {"turns": "many", "cost_usd": None, "success_rate": "n/a"}}, "old-1": {"rollup": "nope"}, "Cindi-9f49": "garbage"}})
        return httpx.Response(200, json={"warnings": []})

    be = deckdata.LiveBackend(_conn(handler))
    snap = be.snapshot()
    assert not snap.error and len(snap.roster) == len(ROSTER)
    assert snap.rollups["protoEngineer-ba4c"].turns == 0 and snap.rollups["protoEngineer-ba4c"].cost_usd == 0.0
    assert snap.rollups["old-1"].cost_usd == 0.0

    def handler_list(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/telemetry/fleet":
            return httpx.Response(200, json=[1, 2, 3])
        return handler(request)

    be = deckdata.LiveBackend(_conn(handler_list))
    be.snapshot()
    snap = be.snapshot()
    assert not snap.error and snap.rollups == {}


def test_live_detail_notes_a_disabled_log_buffer_and_rejects_non_dict_bodies():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/api/diagnostics/logs"):
            return httpx.Response(200, json={"enabled": False, "lines": [], "returned": 0, "capacity": 0, "note": "log buffer disabled (LOG_BUFFER_LINES=0)"})
        if p.endswith("/api/runtime/status"):
            return httpx.Response(200, text="<html>proxy</html>")
        return httpx.Response(200, json={"sessions": []})

    be = deckdata.LiveBackend(_conn(handler))
    d = be.detail(ROSTER[1])
    assert d.logs == [] and d.logs_note == "log buffer disabled (LOG_BUFFER_LINES=0)" and not d.logs_error
    assert "malformed" in d.runtime_error and d.runtime == {}

    def handler_no_note(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/diagnostics/logs"):
            return httpx.Response(200, json={"enabled": False, "lines": []})
        return httpx.Response(200, json={"sessions": [], "warnings": []})

    d = deckdata.LiveBackend(_conn(handler_no_note)).detail(ROSTER[1])
    assert d.logs_note == "log buffer disabled on this member"


def test_live_detail_host_reads_the_hub_directly_and_member_401_is_scoped():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.startswith("/agents/old-1/"):
            return httpx.Response(401)
        if p == "/api/runtime/status":
            return httpx.Response(200, json={"model": {"name": "m"}})
        return httpx.Response(200, json={"enabled": True, "lines": [], "sessions": []})

    be = deckdata.LiveBackend(_conn(handler))
    host = be.detail(ROSTER[0])
    assert host.runtime == {"model": {"name": "m"}} and not host.runtime_error
    member = be.detail(ROSTER[2])
    assert "member 'old-1' rejected" in member.runtime_error
    assert "member 'old-1' rejected" in member.logs_error


def test_offline_backend_drops_the_host_row_and_has_no_detail(tmp_path):
    calls: list[tuple] = []
    disk = [
        {"name": "main", "id": "main", "port": 7870, "pid": 999, "running": True, "host": True},
        {"name": "alpha", "id": "alpha-1", "port": 7901, "pid": None, "running": False},
    ]
    be = deckdata.OfflineBackend(
        status=lambda: list(disk),
        start=lambda n: calls.append(("start", n)) or {"name": n, "port": 7901, "pid": 5},
        stop=lambda n: calls.append(("stop", n)) or {"name": n, "stopped": True},
        fleet_json=tmp_path / "fleet.json",
        reason="no hub answered",
    )
    snap = be.snapshot()
    assert snap.mode == "offline" and [a["name"] for a in snap.roster] == ["alpha"]
    assert snap.label == f"offline · no hub answered · reading {tmp_path / 'fleet.json'}"
    be.start("alpha")
    be.stop("alpha")
    assert calls == [("start", "alpha"), ("stop", "alpha")]
    d = be.detail(disk[1])
    assert "offline" in d.runtime_error and "offline" in d.logs_error
    assert be.console_href(disk[1]) is None


def test_offline_snapshot_status_failure_is_reported(tmp_path):
    def boom():
        raise RuntimeError("fleet.json unreadable")

    be = deckdata.OfflineBackend(status=boom, start=lambda n: {}, stop=lambda n: {}, fleet_json=Path("/x/fleet.json"))
    snap = be.snapshot()
    assert snap.roster == [] and "unreadable" in snap.error
