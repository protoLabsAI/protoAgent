"""Guarded read-only fleet diagnostics tool (#3170, ADR 0071).

Covers the acceptance criteria that are the tool's own: config gating (default-off model
exposure), roster-only resolution + operator auth, the request being a bounded read-only GET
to the existing ``/agents/{slug}/api/diagnostics/*`` proxy path, each failure mode returning a
compact object rather than raising, secret redaction, and output bounding/truncation.

The member-local endpoints (#3168) and their own bounds/auth are tested in
``tests/test_diagnostics_routes.py``; here the member side is a ``MockTransport`` so the tool's
own behavior is exercised without a live hub.
"""

from __future__ import annotations

import json

import httpx
import pytest

import tools.fleet_diagnostics as fd
from graph.config import LangGraphConfig
from tools.lg_tools import get_all_tools


# ── config gating (r3) ───────────────────────────────────────────────────────


def test_default_config_does_not_expose_the_tool():
    """Default config leaves the tool UNBOUND — an ordinary agent never sees it."""
    assert LangGraphConfig().fleet_diagnostics_enabled is False
    names = {t.name for t in get_all_tools()}
    assert "fleet_diagnostics" not in names


def test_enabled_flag_binds_the_tool():
    names = {t.name for t in get_all_tools(fleet_diagnostics_enabled=True)}
    assert "fleet_diagnostics" in names


def test_config_parses_the_enable_flag():
    on = LangGraphConfig.from_dict({"fleet": {"diagnostics": {"enabled": True}}})
    assert on.fleet_diagnostics_enabled is True
    # Absent / empty blocks keep the default off (the gate is opt-in).
    assert LangGraphConfig.from_dict({}).fleet_diagnostics_enabled is False
    assert LangGraphConfig.from_dict({"fleet": {"diagnostics": {}}}).fleet_diagnostics_enabled is False


# ── harness: a roster + a MockTransport member ───────────────────────────────


class _Env:
    def __init__(self, calls, responder):
        self.calls = calls
        self._responder = responder

    def respond(self, fn):
        """Set the member's reply (``fn(request) -> httpx.Response`` — may raise to simulate
        a transport failure)."""
        self._responder["fn"] = fn


@pytest.fixture
def env(monkeypatch):
    import graph.fleet.service_token as service_token
    import graph.fleet.supervisor as supervisor
    import graph.workspaces.manager as manager
    from runtime.state import STATE

    calls: list[httpx.Request] = []
    responder = {"fn": lambda req: httpx.Response(200, json={"enabled": True, "returned": 0, "lines": []})}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responder["fn"](request)

    monkeypatch.setattr(fd, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    # A two-kind roster: one local workspace, one registered remote.
    monkeypatch.setattr(manager, "list_workspaces", lambda: [{"id": "w-roxy", "name": "roxy"}])
    monkeypatch.setattr(supervisor, "list_remotes", lambda: [{"id": "r-gina", "name": "gina", "url": "http://gina"}])
    monkeypatch.setattr(service_token, "resolve_service_token", lambda: "fleet-secret")
    monkeypatch.setattr(STATE, "active_port", 7899, raising=False)
    return _Env(calls, responder)


async def _run(**kwargs) -> dict:
    return json.loads(await fd.fleet_diagnostics.ainvoke(kwargs))


# ── roster resolution + auth + the exact proxy path (r1, r2) ─────────────────


async def test_logs_success_hits_the_proxy_path_with_operator_auth(env):
    env.respond(lambda req: httpx.Response(200, json={"enabled": True, "returned": 1, "lines": [{"message": "hi"}]}))
    out = await _run(member="roxy", what="logs", lines=50)

    assert out["ok"] is True
    assert out["member"] == "roxy"
    assert out["kind"] == "logs"
    assert out["data"]["lines"] == [{"message": "hi"}]

    req = env.calls[-1]
    assert req.method == "GET"
    assert req.url.path == "/agents/w-roxy/api/diagnostics/logs"  # the existing #3168 proxy path
    assert req.headers["authorization"] == "Bearer fleet-secret"  # operator-tier fleet token


async def test_member_resolves_by_id_and_remote_by_name(env):
    await _run(member="w-roxy")  # local workspace, by id
    assert env.calls[-1].url.path == "/agents/w-roxy/api/diagnostics/logs"
    await _run(member="gina")  # remote member, by display name
    assert env.calls[-1].url.path == "/agents/r-gina/api/diagnostics/logs"


async def test_unknown_member_is_refused_without_any_request(env):
    out = await _run(member="ghost")
    assert out["ok"] is False
    assert out["error"] == "unknown member"
    assert set(out["known_members"]) == {"roxy", "gina"}
    assert env.calls == []  # no host/URL is ever contacted for an unregistered member


async def test_tool_never_targets_an_arbitrary_url(env):
    """A URL-shaped selector is just an unknown roster name — not an address to dial (r2)."""
    out = await _run(member="http://evil.example/api")
    assert out["ok"] is False and out["error"] == "unknown member"
    assert env.calls == []


async def test_task_lookup_hits_the_exact_task_path(env):
    env.respond(lambda req: httpx.Response(200, json={"task_id": "task-1", "state": "completed"}))
    out = await _run(member="roxy", what="task", task_id="task-1")
    assert out["ok"] is True and out["kind"] == "task"
    assert out["data"]["task_id"] == "task-1"
    assert env.calls[-1].url.path == "/agents/w-roxy/api/diagnostics/tasks/task-1"


# ── read-only: only ever a GET to a diagnostics path (r6) ────────────────────


async def test_only_read_only_diagnostics_gets_are_issued(env):
    await _run(member="roxy", what="logs")
    await _run(member="roxy", what="task", task_id="t1")
    assert env.calls, "expected requests to have been made"
    for req in env.calls:
        assert req.method == "GET"  # never POST/PUT/DELETE — no start/resume/answer/mutate path
        assert "/api/diagnostics/" in req.url.path


# ── caller-bounded line selector + output bounds (r4) ────────────────────────


@pytest.mark.parametrize(("asked", "sent"), [(99999, "1000"), (0, "1"), (-5, "1"), (50, "50")])
async def test_lines_is_clamped_before_it_leaves(env, asked, sent):
    await _run(member="roxy", what="logs", lines=asked)
    assert env.calls[-1].url.params.get("lines") == sent


async def test_output_is_bounded_and_flags_truncation(env, monkeypatch):
    monkeypatch.setattr(fd, "_MAX_OUTPUT_CHARS", 400)
    big = {"enabled": True, "returned": 40, "lines": [{"message": f"line {i} " + "x" * 40} for i in range(40)]}
    env.respond(lambda req: httpx.Response(200, json=big))
    raw = await fd.fleet_diagnostics.ainvoke({"member": "roxy", "what": "logs"})
    out = json.loads(raw)
    assert len(raw) <= fd._MAX_OUTPUT_CHARS  # the hard ceiling holds
    assert out["output_truncated"] is True  # …and it SAYS the view is partial
    assert out["ok"] is True


async def test_server_truncation_and_malformed_signals_pass_through(env):
    env.respond(
        lambda req: httpx.Response(
            200, json={"task_id": "t", "truncated": ["history"], "malformed": ["status"], "state": None}
        )
    )
    out = await _run(member="roxy", what="task", task_id="t")
    assert out["data"]["truncated"] == ["history"]
    assert out["data"]["malformed"] == ["status"]


async def test_secrets_are_redacted_from_the_output(env):
    leak = "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345"
    env.respond(lambda req: httpx.Response(200, json={"enabled": True, "lines": [{"message": leak}]}))
    raw = await fd.fleet_diagnostics.ainvoke({"member": "roxy", "what": "logs"})
    assert "REDACTED" in raw
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in raw


# ── failure modes → compact actionable objects, never a raise (r5) ───────────


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not found"),
        (409, "member not running"),
        (502, "member unreachable"),
        (503, "member store unavailable"),
        (504, "member did not respond in time"),
        (500, "request failed"),
    ],
)
async def test_proxy_and_member_status_codes_map_to_compact_errors(env, status, error):
    env.respond(lambda req: httpx.Response(status, json={"detail": "upstream said so"}))
    out = await _run(member="roxy", what="logs")
    assert out["ok"] is False
    assert out["error"] == error
    assert out["status"] == status
    assert out["member"] == "roxy"


async def test_unreachable_member_is_contained(env):
    def boom(req):
        raise httpx.ConnectError("connection refused", request=req)

    env.respond(boom)
    out = await _run(member="roxy", what="logs")
    assert out["ok"] is False and out["error"] == "member unreachable"


async def test_slow_member_times_out_cleanly(env):
    def stall(req):
        raise httpx.ReadTimeout("too slow", request=req)

    env.respond(stall)
    out = await _run(member="roxy", what="logs")
    assert out["ok"] is False and out["error"] == "member did not respond in time"


async def test_missing_task_id_is_refused_before_any_request(env):
    out = await _run(member="roxy", what="task", task_id="  ")
    assert out["ok"] is False and out["error"] == "task_id required"
    assert env.calls == []


async def test_invalid_what_is_refused(env):
    out = await _run(member="roxy", what="restart")
    assert out["ok"] is False and out["error"] == "invalid 'what'"
    assert env.calls == []


async def test_hub_without_a_resolvable_port_fails_compactly(env, monkeypatch):
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "active_port", None, raising=False)
    out = await _run(member="roxy", what="logs")
    assert out["ok"] is False and out["error"] == "hub unavailable"
    assert env.calls == []  # never even attempted


async def test_empty_member_is_refused(env):
    out = await _run(member="   ")
    assert out["ok"] is False and out["error"] == "no member given"
    assert env.calls == []
