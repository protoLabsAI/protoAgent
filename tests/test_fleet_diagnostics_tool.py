"""Guarded read-only fleet-diagnostics tool (ADR 0071, #3170).

Covers the tool's own contract: the config gate (default off), roster-only resolution + the
auth it attaches, each network/HTTP failure mode surfacing as a compact structured answer,
output boundedness + preservation of the member's own truncated/malformed signals, secret
redaction, and that the only verbs are two GETs under ``/api/diagnostics`` (read-only, no
alternate host/URL path).

The member endpoints themselves (#3168) are tested in ``test_diagnostics_routes.py``; here the
member is a MockTransport so the tool is exercised without a live fleet.
"""

from __future__ import annotations

import json

import httpx
import pytest

import tools.fleet_diagnostics as fd

# ── fixtures / helpers ────────────────────────────────────────────────────────

_LOCAL = {"id": "alpha", "name": "Alpha", "running": True, "port": 7871}
_HOST = {"id": "host-id", "name": "main", "host": True, "running": True, "port": 7870}
_REMOTE = {"id": "rem1", "name": "Remote", "remote": True, "running": True, "url": "https://peer.example/", "port": None}


def _install_roster(monkeypatch, entries, remotes=None):
    """Point the tool's roster resolution at a fixed set of entries + remote records, and give
    the fleet service token a deterministic value."""
    from graph.fleet import service_token, supervisor

    monkeypatch.setattr(supervisor, "status", lambda: [dict(e) for e in entries])
    monkeypatch.setattr(supervisor, "remote_for_slug", lambda slug: (remotes or {}).get(slug))
    monkeypatch.setattr(service_token, "resolve_service_token", lambda: "svc-tok-abc")


def _install_http(monkeypatch, *, status=200, body=None, raise_exc=None, capture=None):
    """Install a MockTransport member. ``raise_exc`` is a callable(request)->Exception to
    simulate transport failures; otherwise a JSON response with ``status`` / ``body``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if raise_exc is not None:
            raise raise_exc(request)
        content = json.dumps({} if body is None else body).encode()
        return httpx.Response(status, content=content, headers={"content-type": "application/json"})

    monkeypatch.setattr(fd, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _forbid_http(monkeypatch):
    """Make any HTTP attempt fail loudly — proves resolution failures never reach the network."""

    def _boom():
        raise AssertionError("no HTTP call should be made for this case")

    monkeypatch.setattr(fd, "_make_client", _boom)


async def _run(member, *, kind="logs", task_id="", lines=fd._DEFAULT_LINES):
    return json.loads(await fd._run(member, kind, task_id, lines))


# ── r3: config gating ─────────────────────────────────────────────────────────


def test_default_config_does_not_expose_the_tool():
    from graph.config import LangGraphConfig
    from tools.lg_tools import get_all_tools

    cfg = LangGraphConfig()
    assert cfg.fleet_diagnostics_enabled is False  # default OFF
    names = {t.name for t in get_all_tools(graph_config=cfg)}
    assert "fleet_diagnostics" not in names


def test_enabled_config_binds_the_tool():
    from graph.config import LangGraphConfig
    from tools.lg_tools import get_all_tools

    cfg = LangGraphConfig()
    cfg.fleet_diagnostics_enabled = True
    tools = {t.name: t for t in get_all_tools(graph_config=cfg)}
    assert "fleet_diagnostics" in tools


def test_no_graph_config_never_builds_the_tool():
    # The operator-MCP build path passes no graph_config — the tool must be structurally absent.
    from tools.lg_tools import get_all_tools

    assert "fleet_diagnostics" not in {t.name for t in get_all_tools()}


def test_agent_cannot_self_enable_the_toggle_via_set_config():
    """The guarded self-config writer must refuse `fleet.diagnostics.enabled` — a capability
    toggle is the operator's to flip, never the agent's (ADR 0071, no self-widening; same
    fence as soul.self_edit_enabled / tools.self_config_enabled)."""
    import asyncio

    from tools.lg_tools import _build_config_editor_tool

    (tool,) = _build_config_editor_tool()
    out = asyncio.run(tool.ainvoke({"updates": {"fleet.diagnostics.enabled": True}}))
    assert out.startswith("Refused:")


def test_built_tool_shape():
    built = fd.build_fleet_diagnostics_tool()
    assert [t.name for t in built] == ["fleet_diagnostics"]


# ── r2: roster-only addressing ─────────────────────────────────────────────────


async def test_unknown_member_is_rejected_and_makes_no_call(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _forbid_http(monkeypatch)
    out = await _run("ghost")
    assert out["ok"] is False
    assert out["error"] == "unknown_member"
    assert out["available"] == ["Alpha"]  # names it back so the model can correct
    assert out["member"] == "ghost"


async def test_stopped_member_is_not_running_and_makes_no_call(monkeypatch):
    _install_roster(monkeypatch, [{"id": "beta", "name": "Beta", "running": False, "port": 7872}])
    _forbid_http(monkeypatch)
    out = await _run("Beta")
    assert out["ok"] is False and out["error"] == "not_running"


async def test_member_matched_by_id_and_by_name(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 10})
    by_name = await _run("Alpha")
    by_id = await _run("alpha")
    assert by_name["ok"] and by_id["ok"]
    assert by_name["member"] == "Alpha"


# ── r1 + auth: local peer reached on loopback with the fleet service token ──────


async def test_local_logs_uses_loopback_and_service_token(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"enabled": True, "lines": [{"message": "hi"}], "returned": 1, "capacity": 2}, capture=cap)
    out = await _run("Alpha", lines=50)
    assert out["ok"] is True
    assert out["data"]["returned"] == 1
    (req,) = cap
    assert req.method == "GET"
    assert str(req.url).startswith("http://127.0.0.1:7871/api/diagnostics/logs")
    assert req.url.params.get("lines") == "50"
    assert req.headers["authorization"] == "Bearer svc-tok-abc"


async def test_host_entry_is_reachable(monkeypatch):
    _install_roster(monkeypatch, [_HOST])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 1}, capture=cap)
    out = await _run("main")
    assert out["ok"] is True
    assert str(cap[0].url).startswith("http://127.0.0.1:7870/api/diagnostics/logs")


async def test_remote_member_uses_its_stored_bearer(monkeypatch):
    _install_roster(monkeypatch, [_REMOTE], remotes={"rem1": {"url": "https://peer.example", "token": "remote-tok"}})
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 1}, capture=cap)
    out = await _run("Remote")
    assert out["ok"] is True
    (req,) = cap
    assert str(req.url).startswith("https://peer.example/api/diagnostics/logs")
    assert req.headers["authorization"] == "Bearer remote-tok"  # its own bearer, not the service token


# ── r1: one exact task by id; r2/r6: quoted path can't escape ──────────────────


async def test_task_fetch_by_id(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"task_id": "t-1", "state": "working", "history": [], "artifacts": []}, capture=cap)
    out = await _run("Alpha", kind="task", task_id="t-1")
    assert out["ok"] is True and out["data"]["state"] == "working"
    assert str(cap[0].url).endswith("/api/diagnostics/tasks/t-1")


async def test_task_id_is_quoted_to_a_single_segment(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, status=404, body={"detail": "no such task"}, capture=cap)
    await _run("Alpha", kind="task", task_id="../chat")
    raw = cap[0].url.raw_path
    assert raw.startswith(b"/api/diagnostics/tasks/")
    assert b"/../" not in raw  # not path traversal…
    assert b"%2F" in raw  # …the separator was percent-encoded into the id segment


async def test_task_requires_a_task_id(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _forbid_http(monkeypatch)  # the guard fires before any call
    out = await _run("Alpha", kind="task", task_id="")
    assert out["ok"] is False and out["error"] == "bad_request"


async def test_unknown_kind_is_rejected(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _forbid_http(monkeypatch)
    out = await _run("Alpha", kind="delete")
    assert out["ok"] is False and out["error"] == "bad_request"


# ── r5: each failure mode → a compact actionable answer, never an exception ─────


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, "unauthorized"), (403, "unauthorized"), (404, "not_found"), (409, "not_running"), (503, "unavailable"), (504, "timeout")],
)
async def test_http_status_maps_to_compact_error(monkeypatch, status, error):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, status=status, body={"detail": "server says so"})
    out = await _run("Alpha", kind="task", task_id="t-1")
    assert out["ok"] is False
    assert out["error"] == error
    assert out["detail"] == "server says so"  # the member's own detail is surfaced
    assert out["status"] == status


async def test_unreachable_member(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, raise_exc=lambda req: httpx.ConnectError("refused", request=req))
    out = await _run("Alpha")
    assert out["ok"] is False and out["error"] == "unreachable"


async def test_timeout_member(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, raise_exc=lambda req: httpx.ReadTimeout("slow", request=req))
    out = await _run("Alpha")
    assert out["ok"] is False and out["error"] == "timeout"


async def test_non_json_2xx_is_a_malformed_answer_not_a_crash(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])

    def handler(request):
        return httpx.Response(200, content=b"not json at all", headers={"content-type": "text/plain"})

    monkeypatch.setattr(fd, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await _run("Alpha")
    assert out["ok"] is True
    assert out["data"]["malformed"] == ["response_not_json"]


# ── r4: bounded output, preserved server signals, redaction ────────────────────


async def test_lines_clamped_to_the_ceiling(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 1}, capture=cap)
    out = await _run("Alpha", lines=99999)
    assert cap[0].url.params.get("lines") == str(fd._MAX_LINES)  # clamped before it left
    assert "above maximum" in out["note"]


async def test_server_truncation_and_malformed_signals_are_preserved(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    body = {
        "task_id": "t-1",
        "state": "working",
        "history": [],
        "artifacts": [],
        "truncated": ["history", "accumulated_text"],
        "malformed": ["status"],
    }
    _install_http(monkeypatch, body=body)
    out = await _run("Alpha", kind="task", task_id="t-1")
    assert out["data"]["truncated"] == ["history", "accumulated_text"]
    assert out["data"]["malformed"] == ["status"]


async def test_long_task_text_is_capped_at_the_tool_boundary(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    big = "x" * (fd._TOOL_TEXT_CAP * 3)
    _install_http(monkeypatch, body={"task_id": "t-1", "accumulated_text": big, "history": [], "artifacts": []})
    out = await _run("Alpha", kind="task", task_id="t-1")
    assert len(out["data"]["accumulated_text"]) == fd._TOOL_TEXT_CAP
    assert out["data"]["tool_truncated"] is True


async def test_output_is_bounded_and_marks_truncation(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    # Far more log volume than the tool's output bound — each line already within the text cap.
    lines = [{"message": "y" * 500, "level": "INFO", "logger": "probe"} for _ in range(400)]
    _install_http(monkeypatch, body={"enabled": True, "lines": lines, "returned": len(lines), "capacity": 2000})
    raw = await fd._run("Alpha", "logs", "", 1000)
    assert len(raw) <= fd._MAX_OUTPUT_CHARS  # hard bound honored
    out = json.loads(raw)
    assert out["tool_truncated"] is True
    assert out["tool_dropped_entries"] > 0
    assert len(out["data"]["lines"]) < 400  # trailing entries dropped to fit


async def test_secrets_are_redacted_at_the_tool_boundary(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    leak = "token sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    _install_http(monkeypatch, body={"enabled": True, "lines": [{"message": leak}], "returned": 1, "capacity": 2})
    raw = await fd._run("Alpha", "logs", "", 10)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in raw
    assert "[REDACTED]" in raw


# ── end-to-end through the LangChain tool wrapper ──────────────────────────────


async def test_tool_ainvoke_end_to_end(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 1})
    tool = fd.build_fleet_diagnostics_tool()[0]
    out = json.loads(await tool.ainvoke({"member": "Alpha", "kind": "logs"}))
    assert out["ok"] is True and out["member"] == "Alpha"
