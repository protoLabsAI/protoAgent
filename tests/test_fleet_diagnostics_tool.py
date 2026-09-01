"""Guarded read-only fleet diagnostics tool (#3170, ADR 0071).

Covers the trust boundary end to end with a mock httpx transport standing in for the
member-local diagnostics API (#3168): config gating (default off), roster-only addressing
+ operator auth, each failure mode, output bounding + signal preservation + redaction, and
the read-only guarantee (GET-only, no alternate host/URL path).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from graph.config import LangGraphConfig
from tools.fleet_diagnostics import (
    _MAX_OUTPUT_CHARS,
    _MAX_TEXT_CHARS,
    FLEET_DIAGNOSTICS_TOOL_NAME,
    build_fleet_diagnostics_tools,
    run_fleet_diagnostics,
)

# host + one local member + one remote member — the roster status() returns.
_ROSTER = [
    {"id": "host-id", "name": "main", "running": True, "host": True},
    {"id": "board-id", "name": "board", "running": True, "port": 7871},
    {"id": "roxy-id", "name": "roxy", "remote": True, "running": True, "url": "http://10.0.0.9:7870"},
]
_TOKEN = "fleet-secret-token-xyz"


@pytest.fixture(autouse=True)
def _fleet_env(monkeypatch):
    """A resolvable roster, a known hub port, and a stable service token."""
    from runtime.state import STATE

    monkeypatch.setattr("graph.fleet.supervisor.status", lambda: [dict(e) for e in _ROSTER])
    monkeypatch.setattr(STATE, "active_port", 7870, raising=False)
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: _TOKEN)


def _transport(handler, seen: list | None = None):
    """A MockTransport that records every request it sees (for read-only / path asserts)."""

    def _h(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return httpx.MockTransport(_h)


# ── r3: config gating (default off; explicit opt-in) ─────────────────────────────


def test_disabled_by_default_builds_no_tool():
    assert build_fleet_diagnostics_tools(SimpleNamespace()) == []
    assert build_fleet_diagnostics_tools(SimpleNamespace(tools_fleet_diagnostics_enabled=False)) == []
    assert LangGraphConfig().tools_fleet_diagnostics_enabled is False  # the config default


def test_enabled_builds_the_named_tool():
    tools = build_fleet_diagnostics_tools(SimpleNamespace(tools_fleet_diagnostics_enabled=True))
    assert [t.name for t in tools] == [FLEET_DIAGNOSTICS_TOOL_NAME]


def test_get_all_tools_hides_it_by_default_and_binds_it_when_enabled():
    from tools.lg_tools import get_all_tools

    off = {t.name for t in get_all_tools(graph_config=LangGraphConfig())}
    assert FLEET_DIAGNOSTICS_TOOL_NAME not in off

    cfg = LangGraphConfig()
    cfg.tools_fleet_diagnostics_enabled = True
    on = {t.name for t in get_all_tools(graph_config=cfg)}
    assert FLEET_DIAGNOSTICS_TOOL_NAME in on


# ── r1 + r2: roster resolution, operator auth, canonical proxy path ──────────────


async def test_logs_happy_path_hits_canonical_proxy_with_service_token():
    seen: list[httpx.Request] = []

    def handler(request):
        return httpx.Response(200, json={"enabled": True, "capacity": 2000, "returned": 1,
                                         "lines": [{"level": "INFO", "message": "up"}]})

    out = json.loads(await run_fleet_diagnostics("board", transport=_transport(handler, seen)))
    assert out["ok"] is True and out["view"] == "logs" and out["member"] == "board-id"
    assert out["lines"] == [{"level": "INFO", "message": "up"}]

    (req,) = seen
    assert req.method == "GET"  # read-only
    parts = urlsplit(str(req.url))
    assert parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port == 7870
    assert parts.path == "/agents/board-id/api/diagnostics/logs"  # canonical proxy sub-path
    assert req.headers["authorization"] == f"Bearer {_TOKEN}"


async def test_resolves_by_display_name_and_case_insensitively():
    seen: list[httpx.Request] = []
    handler = lambda r: httpx.Response(200, json={"enabled": True, "lines": []})
    # display name → its slug id
    await run_fleet_diagnostics("roxy", transport=_transport(handler, seen))
    assert urlsplit(str(seen[-1].url)).path.startswith("/agents/roxy-id/")
    # case-insensitive
    await run_fleet_diagnostics("ROXY", transport=_transport(handler, seen))
    assert urlsplit(str(seen[-1].url)).path.startswith("/agents/roxy-id/")


async def test_exact_task_by_id():
    seen: list[httpx.Request] = []

    def handler(request):
        return httpx.Response(200, json={"task_id": "t-1", "state": "completed",
                                         "history": [], "artifacts": [], "accumulated_text": "done",
                                         "truncated": [], "malformed": []})

    out = json.loads(await run_fleet_diagnostics("board", view="task", task_id="t-1",
                                                 transport=_transport(handler, seen)))
    assert out["ok"] is True and out["view"] == "task" and out["task_id"] == "t-1"
    assert out["state"] == "completed" and out["accumulated_text"] == "done"
    assert urlsplit(str(seen[-1].url)).path == "/agents/board-id/api/diagnostics/tasks/t-1"


async def test_unknown_member_is_refused_without_any_request():
    def handler(request):  # must never be called
        raise AssertionError("no request may be made for an unregistered member")

    out = json.loads(await run_fleet_diagnostics("ghost", transport=_transport(handler)))
    assert out["ok"] is False and out["error"] == "unknown_member"
    assert set(out["available_members"]) >= {"main", "board", "roxy"}


async def test_task_id_is_percent_encoded_no_path_traversal():
    seen: list[httpx.Request] = []
    handler = lambda r: httpx.Response(404, json={"detail": "no such task"})
    await run_fleet_diagnostics("board", view="task", task_id="../../secret",
                                transport=_transport(handler, seen))
    path = urlsplit(str(seen[-1].url)).path
    assert path.startswith("/agents/board-id/api/diagnostics/tasks/")
    assert "../" not in path and "secret" in path  # encoded, cannot escape the path


async def test_line_selector_is_clamped_and_forwarded():
    seen: list[httpx.Request] = []
    handler = lambda r: httpx.Response(200, json={"enabled": True, "lines": []})
    await run_fleet_diagnostics("board", lines=99999, transport=_transport(handler, seen))
    assert parse_qs(urlsplit(str(seen[-1].url)).query)["lines"] == ["1000"]  # clamped to max
    await run_fleet_diagnostics("board", lines=0, transport=_transport(handler, seen))
    assert parse_qs(urlsplit(str(seen[-1].url)).query)["lines"] == ["1"]  # clamped to min


# ── r4: bounded output, preserved server signals, redaction ──────────────────────


async def test_preserves_server_truncation_and_malformed_signals():
    handler = lambda r: httpx.Response(200, json={
        "task_id": "t", "state": "working", "history": [], "artifacts": [],
        "status_message": "", "accumulated_text": "",
        "truncated": ["history", "accumulated_text"], "malformed": ["artifact_entry"]})
    out = json.loads(await run_fleet_diagnostics("board", view="task", task_id="t",
                                                 transport=_transport(handler)))
    assert set(out["truncated"]) == {"history", "accumulated_text"}
    assert out["malformed"] == ["artifact_entry"]


async def test_redacts_known_secret_forms():
    leak = "Authorization: Bearer supersecretvalue and key sk-ABCDEFGHIJKLMNOPQRSTUVWX"
    handler = lambda r: httpx.Response(200, json={"enabled": True, "lines": [{"message": leak}]})
    raw = await run_fleet_diagnostics("board", transport=_transport(handler))
    assert "supersecretvalue" not in raw and "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in raw
    assert "[REDACTED]" in raw


async def test_long_line_text_is_truncated():
    handler = lambda r: httpx.Response(200, json={"enabled": True,
                                                  "lines": [{"message": "x" * 5000}]})
    out = json.loads(await run_fleet_diagnostics("board", transport=_transport(handler)))
    assert out["line_text_truncated"] is True
    assert len(out["lines"][0]["message"]) <= _MAX_TEXT_CHARS + 40  # cap + marker


async def test_output_is_bounded_under_the_ceiling():
    big = {"enabled": True, "lines": [{"message": "y" * 4000} for _ in range(1000)]}
    handler = lambda r: httpx.Response(200, json=big)
    raw = await run_fleet_diagnostics("board", transport=_transport(handler))
    assert len(raw) <= _MAX_OUTPUT_CHARS
    assert json.loads(raw)["output_truncated"] is True  # still valid JSON, flagged


# ── r5: every failure mode returns a compact, actionable object (never raises) ───


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not_found"),
        (409, "not_running"),
        (502, "unreachable"),
        (503, "unavailable"),
        (504, "timeout"),
        (500, "member_error"),
    ],
)
async def test_http_status_maps_to_actionable_error(status, error):
    handler = lambda r: httpx.Response(status, json={"detail": "member said so"})
    out = json.loads(await run_fleet_diagnostics("board", transport=_transport(handler)))
    assert out["ok"] is False and out["error"] == error and out["status"] == status
    assert out["detail"] == "member said so"


async def test_missing_task_returns_not_found_with_task_id():
    handler = lambda r: httpx.Response(404, json={"detail": "no such task on this member"})
    out = json.loads(await run_fleet_diagnostics("board", view="task", task_id="nope",
                                                 transport=_transport(handler)))
    assert out["error"] == "not_found" and out["task_id"] == "nope"


async def test_connection_error_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    out = json.loads(await run_fleet_diagnostics("board", transport=_transport(handler)))
    assert out["error"] == "unreachable"


async def test_read_timeout_is_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    out = json.loads(await run_fleet_diagnostics("board", transport=_transport(handler)))
    assert out["error"] == "timeout"


async def test_non_json_body_is_bad_response():
    handler = lambda r: httpx.Response(200, text="<html>not json</html>")
    out = json.loads(await run_fleet_diagnostics("board", transport=_transport(handler)))
    assert out["error"] == "bad_response"


async def test_missing_hub_port_refuses_without_a_request(monkeypatch):
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "active_port", None, raising=False)

    def handler(request):
        raise AssertionError("no request may be made when the hub port is unknown")

    out = json.loads(await run_fleet_diagnostics("board", transport=_transport(handler)))
    assert out["error"] == "host_unavailable"


# ── r5/r6: input guards + read-only surface ──────────────────────────────────────


async def test_task_view_requires_task_id():
    handler = lambda r: httpx.Response(200, json={})
    out = json.loads(await run_fleet_diagnostics("board", view="task", transport=_transport(handler)))
    assert out["error"] == "task_id_required"


async def test_unknown_view_is_rejected():
    handler = lambda r: httpx.Response(200, json={})
    out = json.loads(await run_fleet_diagnostics("board", view="delete", transport=_transport(handler)))
    assert out["error"] == "bad_view"


async def test_only_get_requests_are_ever_issued():
    """Read-only: whatever the view, the tool issues GET against a /diagnostics/ path only —
    it can never start, resume, answer, or mutate anything."""
    seen: list[httpx.Request] = []
    handler = lambda r: httpx.Response(200, json={"enabled": True, "lines": [],
                                                  "history": [], "artifacts": []})
    await run_fleet_diagnostics("board", transport=_transport(handler, seen))
    await run_fleet_diagnostics("board", view="task", task_id="t-1", transport=_transport(handler, seen))
    assert seen and all(r.method == "GET" for r in seen)
    assert all("/api/diagnostics/" in urlsplit(str(r.url)).path for r in seen)
