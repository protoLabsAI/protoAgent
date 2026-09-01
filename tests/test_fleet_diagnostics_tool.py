"""Roster-only fleet-member resolution and the bounded log read (ADR 0071, #3170 · slice 2).

Covers this module's own contract: roster-only resolution (an unknown member is refused with no
HTTP call, and there is no host/URL parameter at all), the log read going through the hub's own
``/agents/<slug>/api/diagnostics/logs`` proxy authenticated with the fleet service token, each
network/HTTP failure mode surfacing as a compact structured answer, output boundedness +
preservation of the member's own ``enabled``/``note``/``truncated``/``malformed`` signals, and
secret redaction at the tool boundary.

The member endpoint itself (#3168) is tested in ``test_diagnostics_routes.py``, and the proxy's
per-member routing + credential swap in the proxy's own tests; here the loopback ``/agents/<slug>``
hop is a MockTransport, so the tool is exercised without a live server or fleet.
"""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

import tools.fleet_diagnostics as fd

# ── fixtures / helpers ────────────────────────────────────────────────────────

_LOCAL = {"id": "alpha", "name": "Alpha", "running": True, "port": 7871}
_HOST = {"id": "main-id", "name": "main", "host": True, "running": True, "port": 7870}
_REMOTE = {"id": "rem1", "name": "Remote", "remote": True, "running": True, "url": "https://peer.example/"}


def _install_roster(monkeypatch, entries):
    """Point resolution at a fixed roster, pin the service token, and give this instance a known
    loopback port — so the tool's loopback URL is deterministic."""
    from graph.fleet import service_token, supervisor
    from runtime.state import STATE

    monkeypatch.setattr(supervisor, "status", lambda: [dict(e) for e in entries])
    monkeypatch.setattr(service_token, "resolve_service_token", lambda: "svc-tok-abc")
    monkeypatch.setattr(STATE, "active_port", 7870)


def _install_http(monkeypatch, *, status=200, body=None, raise_exc=None, capture=None):
    """Install a MockTransport at the loopback proxy boundary. ``raise_exc`` is a
    callable(request)->Exception for transport failures; otherwise a JSON response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if raise_exc is not None:
            raise raise_exc(request)
        content = json.dumps({} if body is None else body).encode()
        return httpx.Response(status, content=content, headers={"content-type": "application/json"})

    monkeypatch.setattr(fd, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _forbid_http(monkeypatch):
    """Make any HTTP attempt fail loudly — proves a refusal never reaches the network."""

    def _boom():
        raise AssertionError("no HTTP call should be made for this case")

    monkeypatch.setattr(fd, "_make_client", _boom)


async def _logs(member, *, lines=fd._DEFAULT_LINES):
    return await fd.read_member_logs(member, lines)


# ── r2: roster-only addressing (no host/URL path) ─────────────────────────────


def test_no_host_or_url_parameter_exists():
    # The public surface accepts a member NAME and a line count — nothing else. There is no
    # parameter (and so no code path) that could accept a host, port, or URL.
    assert set(inspect.signature(fd.read_member_logs).parameters) == {"member", "lines"}
    assert set(inspect.signature(fd.resolve_member).parameters) == {"member"}


async def test_unknown_member_is_refused_and_makes_no_call(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _forbid_http(monkeypatch)
    out = await _logs("ghost")
    assert out["ok"] is False
    assert out["error"] == "unknown_member"
    assert out["member"] == "ghost"
    assert out["available"] == ["Alpha"]  # names it back so the caller can correct


async def test_a_url_shaped_member_is_just_an_unknown_name(monkeypatch):
    # Handing the tool something that LOOKS like an address does not open an address path — it is
    # simply a name the roster does not contain, so it is refused with no call.
    _install_roster(monkeypatch, [_LOCAL])
    _forbid_http(monkeypatch)
    out = await _logs("http://127.0.0.1:9999")
    assert out["ok"] is False and out["error"] == "unknown_member"


async def test_member_matched_by_id_and_by_name(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 10})
    by_name = await _logs("Alpha")
    by_id = await _logs("alpha")
    assert by_name["ok"] and by_id["ok"]
    assert by_name["member"] == "Alpha" and by_name["slug"] == "alpha"


def test_resolve_member_refuses_unknown_and_resolves_registered(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    assert isinstance(fd.resolve_member("ghost"), dict)
    target = fd.resolve_member("Alpha")
    assert isinstance(target, fd._Target)
    assert target.slug == "alpha" and target.member == "Alpha"


# ── r1 + auth: read through the proxy on loopback with the fleet service token ──


async def test_local_logs_go_through_the_proxy_with_the_service_token(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"enabled": True, "lines": [{"message": "hi"}], "returned": 1, "capacity": 2}, capture=cap)
    out = await _logs("Alpha", lines=50)
    assert out["ok"] is True
    assert out["logs"]["returned"] == 1
    (req,) = cap
    assert req.method == "GET"
    # Reaches THIS instance's own /agents/<slug>/* proxy — never the member directly.
    assert str(req.url).startswith("http://127.0.0.1:7870/agents/alpha/api/diagnostics/logs")
    assert req.url.params.get("lines") == "50"  # the caller-bounded selector is forwarded
    assert req.headers["authorization"] == "Bearer svc-tok-abc"


async def test_host_is_reached_through_the_reserved_host_slug(monkeypatch):
    _install_roster(monkeypatch, [_HOST])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 1}, capture=cap)
    out = await _logs("main")
    assert out["ok"] is True and out["slug"] == "host"
    assert str(cap[0].url).startswith("http://127.0.0.1:7870/agents/host/api/diagnostics/logs")


async def test_remote_member_addressed_by_slug_still_presents_the_service_token(monkeypatch):
    # The tool always authenticates to its OWN proxy with the service token; presenting the
    # remote's own bearer is the PROXY's job (tested there), not this tool's.
    _install_roster(monkeypatch, [_REMOTE])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 1}, capture=cap)
    out = await _logs("Remote")
    assert out["ok"] is True and out["slug"] == "rem1"
    (req,) = cap
    assert str(req.url).startswith("http://127.0.0.1:7870/agents/rem1/api/diagnostics/logs")
    assert req.headers["authorization"] == "Bearer svc-tok-abc"


# ── r3: each failure mode → a compact actionable answer, never an exception ─────


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
    ],
)
async def test_http_status_maps_to_a_compact_error(monkeypatch, status, error):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, status=status, body={"detail": "server says so"})
    out = await _logs("Alpha")
    assert out["ok"] is False
    assert out["error"] == error
    assert out["detail"] == "server says so"  # the proxy/member's own detail is surfaced
    assert out["status"] == status


async def test_unreachable_member(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, raise_exc=lambda req: httpx.ConnectError("refused", request=req))
    out = await _logs("Alpha")
    assert out["ok"] is False and out["error"] == "unreachable"


async def test_timeout_member(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, raise_exc=lambda req: httpx.ReadTimeout("slow", request=req))
    out = await _logs("Alpha")
    assert out["ok"] is False and out["error"] == "timeout"


async def test_non_json_2xx_is_a_malformed_answer_not_a_crash(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])

    def handler(request):
        return httpx.Response(200, content=b"not json at all", headers={"content-type": "text/plain"})

    monkeypatch.setattr(fd, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await _logs("Alpha")
    assert out["ok"] is True
    assert out["logs"]["malformed"] == ["response_not_json"]


async def test_no_active_port_is_a_compact_unavailable(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "active_port", None)
    _forbid_http(monkeypatch)  # resolution succeeds, but the loopback base can't be built
    out = await _logs("Alpha")
    assert out["ok"] is False and out["error"] == "unavailable"


# ── r4: bounded output, preserved server signals, redaction ────────────────────


async def test_lines_clamped_to_the_ceiling(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 1}, capture=cap)
    out = await _logs("Alpha", lines=99999)
    assert cap[0].url.params.get("lines") == str(fd._MAX_LINES)  # clamped before it left
    assert "above maximum" in out["note"]


async def test_member_note_and_capacity_are_preserved(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    body = {"enabled": True, "lines": [{"message": "x"}], "returned": 1, "capacity": 500, "note": "clamped upstream"}
    _install_http(monkeypatch, body=body)
    out = await _logs("Alpha")
    assert out["logs"]["capacity"] == 500
    assert out["logs"]["returned"] == 1
    assert out["logs"]["note"] == "clamped upstream"  # the member's own note, not swallowed


async def test_disabled_buffer_signal_is_preserved(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(
        monkeypatch,
        body={"enabled": False, "lines": [], "returned": 0, "capacity": 0, "note": "log buffer disabled (LOG_BUFFER_LINES=0)"},
    )
    out = await _logs("Alpha")
    assert out["ok"] is True
    assert out["logs"]["enabled"] is False
    assert "disabled" in out["logs"]["note"]


async def test_server_truncated_and_malformed_signals_are_preserved(monkeypatch):
    # Forward-compatible: whatever signal fields the member sets ride through untouched.
    _install_roster(monkeypatch, [_LOCAL])
    body = {"enabled": True, "lines": [], "returned": 0, "capacity": 1, "truncated": ["lines"], "malformed": ["record"]}
    _install_http(monkeypatch, body=body)
    out = await _logs("Alpha")
    assert out["logs"]["truncated"] == ["lines"]
    assert out["logs"]["malformed"] == ["record"]


async def test_long_line_message_is_capped_at_the_tool_boundary(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    big = "y" * (fd._TOOL_TEXT_CAP * 3)
    _install_http(monkeypatch, body={"enabled": True, "lines": [{"message": big}], "returned": 1, "capacity": 2})
    out = await _logs("Alpha")
    assert len(out["logs"]["lines"][0]["message"]) == fd._TOOL_TEXT_CAP
    assert out["logs"]["tool_truncated"] is True


async def test_secrets_are_redacted_at_the_tool_boundary(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    leak = "token sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    _install_http(monkeypatch, body={"enabled": True, "lines": [{"message": leak}], "returned": 1, "capacity": 2})
    out = await _logs("Alpha")
    dumped = json.dumps(out)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in dumped
    assert "[REDACTED]" in dumped
