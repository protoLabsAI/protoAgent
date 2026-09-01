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


async def _task(member, *, task_id="t-1"):
    return await fd.read_member_task(member, task_id)


def _task_body(**overrides):
    """A minimal well-formed #3168 task view; overrides replace individual fields."""
    body = {
        "task_id": "t-1",
        "context_id": "c-1",
        "state": "completed",
        "status_message": "done",
        "last_updated": "2026-09-01T00:00:00+00:00",
        "history": [{"role": "user", "message_id": "m1", "text": "hello"}],
        "artifacts": [{"artifact_id": "a1", "name": "out", "text": "world"}],
        "accumulated_text": "world",
        "truncated": [],
        "malformed": [],
    }
    body.update(overrides)
    return body


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


async def test_a_duplicated_display_name_is_refused_not_silently_picked(monkeypatch):
    # Display names are operator-editable and NOT unique. Taking the first match would hand back
    # the OTHER agent's logs under the name the caller asked for, with nothing in the answer
    # revealing the substitution — so ambiguity is refused, with the unique ids to re-ask by.
    twin = {"id": "alpha-2", "name": "Alpha", "running": True, "port": 7872}
    _install_roster(monkeypatch, [_LOCAL, twin])
    _forbid_http(monkeypatch)
    out = await _logs("Alpha")
    assert out["ok"] is False and out["error"] == "ambiguous_member"
    assert sorted(out["candidates"]) == ["alpha", "alpha-2"]
    # ...and the id remains unambiguous, so the caller has a way through.
    _install_http(monkeypatch, body={"enabled": True, "lines": [], "returned": 0, "capacity": 10})
    assert (await _logs("alpha-2"))["slug"] == "alpha-2"


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


async def test_non_json_log_preview_is_redacted_before_it_is_bounded(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    monkeypatch.setattr(fd, "_TOOL_TEXT_CAP", 18)
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    def handler(request):
        text = f"tok {secret} " + "Z" * 100
        return httpx.Response(200, content=text.encode(), headers={"content-type": "text/plain"})

    monkeypatch.setattr(fd, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await _logs("Alpha")
    dumped = json.dumps(out)
    assert secret not in dumped
    assert "sk-ABC" not in dumped
    assert out["logs"]["text"] == "tok [REDACTED] ZZZ"


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
    # Defense in depth: even if a member echoes a raw credential into a log line, the tool
    # re-redacts (graph.middleware.redaction.redact) before returning. ``[REDACTED]`` is the
    # replacement MARKER — its presence in the output is the whole point, so we assert the RAW
    # credential is gone and the marker took its place; we never assert the marker is absent.
    _install_roster(monkeypatch, [_LOCAL])
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # an OpenAI-shaped key redact() scrubs
    _install_http(
        monkeypatch,
        body={"enabled": True, "lines": [{"message": f"token {secret}"}], "returned": 1, "capacity": 2},
    )
    out = await _logs("Alpha")
    dumped = json.dumps(out)
    assert secret not in dumped  # the raw credential never leaves the tool
    assert "[REDACTED]" in dumped  # ...it left replaced by the marker
    assert out["logs"]["lines"][0]["message"] == "token [REDACTED]"  # redacted in place


async def test_secrets_are_redacted_on_the_ERROR_path_too(monkeypatch):
    # The failure path returns the member's own ``detail`` string, which never passes through
    # the success-side redact(). An auth rejection is exactly where a member echoes back the
    # credential it just refused, so the same boundary has to hold here.
    _install_roster(monkeypatch, [_LOCAL])
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # an OpenAI-shaped key redact() scrubs
    _install_http(monkeypatch, status=401, body={"detail": f"rejected token {secret}"})
    out = await _logs("Alpha")
    assert out["ok"] is False and out["error"] == "unauthorized"
    assert secret not in json.dumps(out)  # the raw credential never leaves the tool
    assert out["detail"] == "rejected token [REDACTED]"  # ...replaced, and the reason survives


async def test_log_secret_straddling_the_bound_is_redacted_before_truncation(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    monkeypatch.setattr(fd, "_TOOL_TEXT_CAP", 20)
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    message = f"tok {secret} " + "Z" * 100
    _install_http(monkeypatch, body={"enabled": True, "lines": [{"message": message}], "returned": 1, "capacity": 2})
    out = await _logs("Alpha")
    dumped = json.dumps(out)
    assert secret not in dumped
    assert "sk-ABC" not in dumped
    assert out["logs"]["lines"][0]["message"] == "tok [REDACTED] ZZZZZ"
    assert out["logs"]["tool_truncated"] is True


async def test_more_rows_than_requested_are_capped_at_the_tool_boundary(monkeypatch):
    # #3168 clamps the row count on the member's side; this holds when the member does NOT —
    # an older member predating that clamp, or a buggy one. The endpoint returns oldest-first,
    # so the recent END is what a diagnostics read keeps.
    _install_roster(monkeypatch, [_LOCAL])
    rows = [{"message": f"line-{i}"} for i in range(50)]
    _install_http(monkeypatch, body={"enabled": True, "lines": rows, "returned": 50, "capacity": 100})
    out = await _logs("Alpha", lines=5)
    assert len(out["logs"]["lines"]) == 5
    assert [r["message"] for r in out["logs"]["lines"]] == [f"line-{i}" for i in range(45, 50)]
    assert out["logs"]["tool_truncated"] is True
    assert out["logs"]["returned"] == 50  # the member's own count is NOT rewritten


# ── r1: one exact task by id, read through the proxy on loopback ────────────────


async def test_exact_task_by_id_is_returned_for_a_registered_member(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body=_task_body(), capture=cap)
    out = await _task("Alpha", task_id="t-1")
    assert out["ok"] is True
    assert out["member"] == "Alpha" and out["slug"] == "alpha" and out["task_id"] == "t-1"
    task = out["task"]
    assert task["state"] == "completed"
    assert task["history"] == [{"role": "user", "message_id": "m1", "text": "hello"}]
    assert task["artifacts"][0]["text"] == "world"
    assert task["accumulated_text"] == "world"
    assert "tool_truncated" not in task  # nothing over-bound, so no tool-side trim signalled
    (req,) = cap
    assert req.method == "GET"
    # The exact task id is the final path segment; reaches THIS instance's /agents/<slug>/* proxy.
    assert str(req.url).startswith("http://127.0.0.1:7870/agents/alpha/api/diagnostics/tasks/t-1")
    assert req.headers["authorization"] == "Bearer svc-tok-abc"


async def test_task_id_is_quoted_to_a_single_path_segment(monkeypatch):
    # A slash-bearing id can't escape the tasks route — it is percent-encoded to one segment.
    _install_roster(monkeypatch, [_LOCAL])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body=_task_body(), capture=cap)
    await _task("Alpha", task_id="ns/../secret")
    assert "/agents/alpha/api/diagnostics/tasks/ns%2F..%2Fsecret" in str(cap[0].url)


async def test_host_task_is_reached_through_the_reserved_host_slug(monkeypatch):
    _install_roster(monkeypatch, [_HOST])
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body=_task_body(), capture=cap)
    out = await _task("main", task_id="t-9")
    assert out["ok"] is True and out["slug"] == "host"
    assert str(cap[0].url).startswith("http://127.0.0.1:7870/agents/host/api/diagnostics/tasks/t-9")


# ── r2: a missing task / bad input is a compact actionable failure ─────────────


async def test_missing_task_is_a_compact_not_found(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, status=404, body={"detail": "no such task on this member", "task_id": "t-x"})
    out = await _task("Alpha", task_id="t-x")
    assert out["ok"] is False
    assert out["error"] == "not_found"
    assert out["detail"] == "no such task on this member"  # the member's own reason is surfaced
    assert out["status"] == 404
    assert out["task_id"] == "t-x"  # ...and the id it was about, so the caller can correct


async def test_task_store_unconfigured_is_a_compact_unavailable(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, status=503, body={"detail": "task store is not configured on this member"})
    out = await _task("Alpha")
    assert out["ok"] is False and out["error"] == "unavailable"
    assert out["status"] == 503


async def test_empty_task_id_is_refused_and_makes_no_call(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _forbid_http(monkeypatch)
    out = await _task("Alpha", task_id="   ")
    assert out["ok"] is False and out["error"] == "invalid_task_id"
    assert out["member"] == "Alpha" and out["task_id"] == ""  # input context is preserved


async def test_unknown_member_on_task_path_is_refused_and_makes_no_call(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    _forbid_http(monkeypatch)
    out = await _task("ghost", task_id="t-1")
    assert out["ok"] is False and out["error"] == "unknown_member"
    assert out["member"] == "ghost" and out["task_id"] == "t-1"


# ── r3: redaction at the DESTINATION on both the success and error task paths ───


async def test_task_secrets_are_redacted_at_the_tool_boundary(monkeypatch):
    # Defense in depth: even if the member echoes a raw credential into task history / output, the
    # tool re-redacts at its own boundary (the DESTINATION) — it does not trust the source.
    _install_roster(monkeypatch, [_LOCAL])
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # an OpenAI-shaped key redact() scrubs
    body = _task_body(
        history=[{"role": "assistant", "message_id": "m1", "text": f"key {secret}"}],
        artifacts=[{"artifact_id": "a1", "name": "out", "text": f"leaked {secret}"}],
        accumulated_text=f"result {secret}",
    )
    _install_http(monkeypatch, body=body)
    out = await _task("Alpha")
    dumped = json.dumps(out)
    assert secret not in dumped  # the raw credential never leaves the tool, on any field
    assert out["task"]["history"][0]["text"] == "key [REDACTED]"
    assert out["task"]["artifacts"][0]["text"] == "leaked [REDACTED]"
    assert out["task"]["accumulated_text"] == "result [REDACTED]"


async def test_task_secrets_are_redacted_on_the_ERROR_path_too(monkeypatch):
    # A 401's ``detail`` never passes through the success-side redact(); an auth rejection is
    # exactly where a member echoes back the credential it just refused, so it is redacted here.
    _install_roster(monkeypatch, [_LOCAL])
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    _install_http(monkeypatch, status=401, body={"detail": f"rejected token {secret}"})
    out = await _task("Alpha")
    assert out["ok"] is False and out["error"] == "unauthorized"
    assert secret not in json.dumps(out)
    assert out["detail"] == "rejected token [REDACTED]"


async def test_secret_straddling_the_bound_is_redacted_before_truncation(monkeypatch):
    # "Redaction matches the DESTINATION, not the source": redaction runs on the FULL text BEFORE
    # the tool's own truncation, so a secret sitting across the truncation boundary is scrubbed
    # whole — never left as a recognizable fragment. Here the cap (20) falls mid-secret, leaving
    # only a "sk-ABCDEFGHIJK" prefix if one truncated FIRST — too short for redact()'s pattern to
    # match, so it would leak. Redacting the full text first replaces the whole token, so it can't.
    _install_roster(monkeypatch, [_LOCAL])
    monkeypatch.setattr(fd, "_TASK_TEXT_CAP", 20)
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    accumulated = f"tok {secret} " + "Z" * 100  # the secret straddles the 20-char cap
    _install_http(monkeypatch, body=_task_body(accumulated_text=accumulated))
    out = await _task("Alpha")
    dumped = json.dumps(out)
    assert secret not in dumped  # the full credential never leaves
    assert "sk-ABC" not in dumped  # ...nor the prefix fragment a naive truncate-first would keep
    assert "[REDACTED]" in dumped  # it left replaced by the marker
    assert len(out["task"]["accumulated_text"]) == 20
    assert out["task"]["tool_truncated"] is True


# ── r4: bounded task output, preserved server signals ──────────────────────────


async def test_task_history_is_capped_at_the_tool_boundary_keeping_the_recent_tail(monkeypatch):
    # #3168 clamps history on the member's side; this holds when the member does NOT. The endpoint
    # returns history oldest-first, so the recent TAIL is what a diagnostics read keeps.
    _install_roster(monkeypatch, [_LOCAL])
    history = [{"role": "user", "message_id": f"m{i}", "text": f"h-{i}"} for i in range(fd._MAX_TASK_HISTORY + 10)]
    _install_http(monkeypatch, body=_task_body(history=history, truncated=["history"]))
    out = await _task("Alpha")
    kept = out["task"]["history"]
    assert len(kept) == fd._MAX_TASK_HISTORY
    assert kept[-1]["message_id"] == f"m{fd._MAX_TASK_HISTORY + 9}"  # the recent end is retained
    assert out["task"]["tool_truncated"] is True
    assert out["task"]["truncated"] == ["history"]  # the member's own signal is NOT rewritten


async def test_task_artifacts_are_capped_at_the_tool_boundary_keeping_the_head(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    artifacts = [{"artifact_id": f"a{i}", "name": "out", "text": f"x{i}"} for i in range(fd._MAX_TASK_ARTIFACTS + 5)]
    _install_http(monkeypatch, body=_task_body(artifacts=artifacts))
    out = await _task("Alpha")
    kept = out["task"]["artifacts"]
    assert len(kept) == fd._MAX_TASK_ARTIFACTS
    assert kept[0]["artifact_id"] == "a0"  # the member returns artifacts head-first; the head is kept
    assert out["task"]["tool_truncated"] is True


async def test_task_text_field_is_capped_at_the_tool_boundary(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    monkeypatch.setattr(fd, "_TASK_TEXT_CAP", 10)
    _install_http(monkeypatch, body=_task_body(accumulated_text="Z" * 50))
    out = await _task("Alpha")
    assert out["task"]["accumulated_text"] == "Z" * 10
    assert out["task"]["tool_truncated"] is True


async def test_task_malformed_and_state_signals_are_preserved(monkeypatch):
    # Forward-compatible: whatever signal fields the member sets ride through untouched, and a
    # task fully within bounds carries no tool_truncated marker.
    _install_roster(monkeypatch, [_LOCAL])
    _install_http(monkeypatch, body=_task_body(state="failed", malformed=["history_entry"]))
    out = await _task("Alpha")
    assert out["ok"] is True
    assert out["task"]["state"] == "failed"
    assert out["task"]["malformed"] == ["history_entry"]
    assert "tool_truncated" not in out["task"]


async def test_non_json_2xx_task_is_a_malformed_answer_not_a_crash(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])

    def handler(request):
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(fd, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await _task("Alpha")
    assert out["ok"] is True
    assert out["task"]["malformed"] == ["response_not_json"]


async def test_non_json_task_preview_is_redacted_before_it_is_bounded(monkeypatch):
    _install_roster(monkeypatch, [_LOCAL])
    monkeypatch.setattr(fd, "_TOOL_TEXT_CAP", 18)
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    def handler(request):
        text = f"tok {secret} " + "Z" * 100
        return httpx.Response(200, content=text.encode(), headers={"content-type": "text/plain"})

    monkeypatch.setattr(fd, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await _task("Alpha")
    dumped = json.dumps(out)
    assert secret not in dumped
    assert "sk-ABC" not in dumped
    assert out["task"]["text"] == "tok [REDACTED] ZZZ"


def test_task_public_surface_takes_only_member_and_task_id():
    # No host/URL path on the task read either — a member NAME and a task id, nothing else.
    assert set(inspect.signature(fd.read_member_task).parameters) == {"member", "task_id"}
