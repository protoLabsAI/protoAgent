"""Demo-agent LLM tools (#4609 part 2) — binding gate, the stamp-guard refusal, chat over the
loopback proxy, and the token-free create output.

The core ops (:mod:`graph.fleet.demo`) are exercised against the real workspace manager in
``test_fleet_demo_4609.py``. Here we test only the thin model-facing wrappers in
:mod:`tools.fleet_demo_tools`: that they bind only behind the (absent-safe) flag, that every
slug op passes through ``require_demo`` and refuses a non-demo WITHOUT any mutation / HTTP, that
chat posts to the member's ``/api/chat`` through this instance's own ``/agents/<slug>/*`` proxy
with the fleet service token, and that create never leaks the member's scoped token.

The loopback ``/agents/<slug>`` hop is a MockTransport, and the core demo ops are faked, so no
server, fleet, or workspace is touched.
"""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

from graph.config import LangGraphConfig
import graph.fleet.demo as demo
import tools.fleet_demo_tools as fdt
import tools.lg_tools as lg_tools

_DEMO_NAMES = {
    "fleet_demo_create",
    "fleet_demo_configure",
    "fleet_demo_chat",
    "fleet_demo_delete",
    "fleet_demo_list",
}


# ── helpers ────────────────────────────────────────────────────────────────────


def _config(enabled: bool, *, identity_name: str = "pm") -> LangGraphConfig:
    """A LangGraphConfig with the (part-3) demo flag set on the instance — the field itself does
    not exist yet, so the binding reads it absent-safe with ``getattr``."""
    cfg = LangGraphConfig(identity_name=identity_name)
    cfg.tools_fleet_demo_enabled = enabled
    return cfg


def _tools(enabled: bool = True, *, identity_name: str = "pm") -> dict:
    return {t.name: t for t in fdt._build_fleet_demo_tools(_config(enabled, identity_name=identity_name))}


def _install_http(monkeypatch, *, status=200, body=None, capture=None):
    """Install a MockTransport at the loopback proxy boundary for fleet_demo_chat, plus a known
    service token and active port so the loopback URL is deterministic."""
    from graph.fleet import service_token
    from runtime.state import STATE

    monkeypatch.setattr(service_token, "resolve_service_token", lambda: "svc-tok-abc")
    monkeypatch.setattr(STATE, "active_port", 7870)

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        content = json.dumps({} if body is None else body).encode()
        return httpx.Response(status, content=content, headers={"content-type": "application/json"})

    monkeypatch.setattr(fdt, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _forbid_http(monkeypatch):
    """Make any HTTP attempt fail loudly — proves a refusal never reaches the network."""

    def _boom():
        raise AssertionError("no HTTP call should be made for this case")

    monkeypatch.setattr(fdt, "_make_client", _boom)


# ── r1: the binding gate (default off; all five on) ─────────────────────────────


def _bound_names(config=None):
    return {getattr(t, "name", "") for t in lg_tools.get_all_tools(graph_config=config)}


def test_demo_tools_absent_from_the_default_toolset():
    assert _DEMO_NAMES.isdisjoint(_bound_names())
    assert _DEMO_NAMES.isdisjoint(_bound_names(LangGraphConfig()))  # flag field absent → off


def test_all_five_demo_tools_bound_when_enabled():
    bound = _bound_names(_config(True))
    assert _DEMO_NAMES <= bound


# ── r2: the stamp guard refuses a real member with no mutation / no HTTP ─────────


def _refuse_all(monkeypatch):
    """Make require_demo reject every slug (as it would a real, unstamped member), and make the
    mutating core ops blow up if a wrapper ever reaches them past the refusal."""

    def _raise(slug):
        raise demo.NotDemoMember(f"{slug!r} is not a demo member")

    monkeypatch.setattr(demo, "require_demo", _raise)
    for op in ("configure_demo", "delete_demo", "create_demo"):
        monkeypatch.setattr(demo, op, _boom_op(op))


def _boom_op(name):
    def _boom(*a, **k):
        raise AssertionError(f"{name} must not be called for a non-demo slug")

    return _boom


async def test_configure_refuses_non_demo_and_touches_nothing(monkeypatch):
    _refuse_all(monkeypatch)
    _forbid_http(monkeypatch)
    out = await _tools()["fleet_demo_configure"].ainvoke(
        {"slug": "jobCoach", "model_connection": "openai", "model": "gpt-4o"}
    )
    assert out["ok"] is False
    assert out["refused"] == "jobCoach is not a demo member — demo tools never touch real agents"


async def test_delete_refuses_non_demo_and_touches_nothing(monkeypatch):
    _refuse_all(monkeypatch)
    _forbid_http(monkeypatch)
    out = await _tools()["fleet_demo_delete"].ainvoke({"slug": "jobCoach"})
    assert out["ok"] is False
    assert out["refused"] == "jobCoach is not a demo member — demo tools never touch real agents"


async def test_chat_refuses_non_demo_and_makes_no_http_call(monkeypatch):
    _refuse_all(monkeypatch)
    _forbid_http(monkeypatch)  # _make_client raises if a request is ever attempted
    out = await _tools()["fleet_demo_chat"].ainvoke({"slug": "jobCoach", "message": "hi"})
    assert out["ok"] is False
    assert out["refused"] == "jobCoach is not a demo member — demo tools never touch real agents"


async def test_unknown_slug_fails_closed_to_the_refusal(monkeypatch):
    # require_demo fails closed on an unknown slug too (no stamp to confirm) — the wrapper must
    # surface the same refusal, not an exception.
    def _raise(slug):
        raise demo.NotDemoMember("no such member")

    monkeypatch.setattr(demo, "require_demo", _raise)
    _forbid_http(monkeypatch)
    out = await _tools()["fleet_demo_chat"].ainvoke({"slug": "ghost", "message": "hi"})
    assert out["ok"] is False and "ghost" in out["refused"]


# ── r3: chat a demo member through the proxy with the service token ──────────────


async def test_chat_posts_to_member_api_chat_and_returns_reply(monkeypatch):
    monkeypatch.setattr(demo, "require_demo", lambda slug: {"id": slug, "demo": True})  # it IS a demo
    cap: list[httpx.Request] = []
    _install_http(
        monkeypatch,
        body={"response": "hello from the demo", "messages": [], "session_id": "api-xyz"},
        capture=cap,
    )
    out = await _tools()["fleet_demo_chat"].ainvoke(
        {"slug": "demo-basic-abc123", "message": "pitch me", "session_id": "api-xyz"}
    )
    assert out["ok"] is True
    assert out["reply"] == "hello from the demo"
    assert out["session_id"] == "api-xyz"

    (req,) = cap
    assert req.method == "POST"
    # Reaches THIS instance's own /agents/<slug>/api/chat proxy — never the member directly.
    assert str(req.url) == "http://127.0.0.1:7870/agents/demo-basic-abc123/api/chat"
    assert req.headers["authorization"] == "Bearer svc-tok-abc"  # the fleet service token
    sent = json.loads(req.content)
    assert sent == {"message": "pitch me", "session_id": "api-xyz"}


async def test_chat_without_session_id_omits_it_and_mints_server_side(monkeypatch):
    monkeypatch.setattr(demo, "require_demo", lambda slug: {"id": slug, "demo": True})
    cap: list[httpx.Request] = []
    _install_http(monkeypatch, body={"response": "ok", "session_id": "api-minted"}, capture=cap)
    out = await _tools()["fleet_demo_chat"].ainvoke({"slug": "demo-x", "message": "hi"})
    assert out["ok"] is True and out["session_id"] == "api-minted"
    assert json.loads(cap[0].content) == {"message": "hi"}  # blank session id is not forwarded


async def test_chat_maps_a_member_error_status_to_a_compact_failure(monkeypatch):
    monkeypatch.setattr(demo, "require_demo", lambda slug: {"id": slug, "demo": True})
    _install_http(monkeypatch, status=409, body={"detail": "agent 'demo-x' is not running"})
    out = await _tools()["fleet_demo_chat"].ainvoke({"slug": "demo-x", "message": "hi"})
    assert out["ok"] is False
    assert out["error"] == "not_running"
    assert out["detail"] == "agent 'demo-x' is not running"
    assert out["status"] == 409


# ── r4: create carries provenance but NEVER the scoped token ─────────────────────


async def test_create_output_has_provenance_and_no_token(monkeypatch):
    captured: dict = {}

    def _fake_create(archetype, model_connection, model, name=None, *, created_by, **kw):
        captured.update(
            archetype=archetype, model_connection=model_connection, model=model, name=name, created_by=created_by
        )
        return {
            "slug": "demo-basic-abc123",
            "port": 7872,
            "token": "SECRET-SCOPED-BEARER",  # must NOT appear in the tool output
            "archetype_ref": "basic",
            "plugin_versions": {"notes": "1.2.3"},
            "created_at": "2026-10-29T00:00:00+00:00",
        }

    monkeypatch.setattr(demo, "create_demo", _fake_create)
    out = await _tools(identity_name="pm")["fleet_demo_create"].ainvoke(
        {"archetype": "basic", "model_connection": "openai", "model": "gpt-4o"}
    )

    # created_by is stamped with THIS agent's identity name (the PM), from config at bind time.
    assert captured["created_by"] == "pm"
    assert captured["name"] is None  # blank name → core default

    assert out["ok"] is True
    assert out["slug"] == "demo-basic-abc123"
    assert out["port"] == 7872
    assert out["archetype_ref"] == "basic"
    assert out["plugin_versions"] == {"notes": "1.2.3"}
    assert out["created_at"] == "2026-10-29T00:00:00+00:00"
    # The scoped token is kept server-side for chat and never crosses into the model's context.
    assert "token" not in out
    assert "SECRET-SCOPED-BEARER" not in json.dumps(out)


async def test_create_surfaces_a_cap_or_unknown_archetype_refusal(monkeypatch):
    def _raise(*a, **k):
        raise demo.DemoCapReached("the demo cap is 3 live members and 3 are already running")

    monkeypatch.setattr(demo, "create_demo", _raise)
    out = await _tools()["fleet_demo_create"].ainvoke(
        {"archetype": "basic", "model_connection": "openai", "model": "gpt-4o"}
    )
    assert out["ok"] is False
    assert "demo cap" in out["error"]


async def test_create_passes_an_explicit_name_through(monkeypatch):
    captured: dict = {}

    def _fake_create(archetype, model_connection, model, name=None, *, created_by, **kw):
        captured["name"] = name
        return {"slug": "pitch-1", "port": 7872, "token": "t", "archetype_ref": "basic",
                "plugin_versions": {}, "created_at": "now"}

    monkeypatch.setattr(demo, "create_demo", _fake_create)
    await _tools()["fleet_demo_create"].ainvoke(
        {"archetype": "basic", "model_connection": "openai", "model": "gpt-4o", "name": "pitch"}
    )
    assert captured["name"] == "pitch"


# ── configure / list happy paths ────────────────────────────────────────────────


async def test_configure_a_demo_member_returns_what_changed(monkeypatch):
    monkeypatch.setattr(demo, "require_demo", lambda slug: {"id": slug, "demo": True})
    seen: dict = {}

    def _fake_configure(slug, *, model_connection=None, model=None, plugin_settings=None):
        seen.update(slug=slug, model_connection=model_connection, model=model, plugin_settings=plugin_settings)
        return {"slug": slug, "changed": ["model", "plugins"], "plugins": ["notes"]}

    monkeypatch.setattr(demo, "configure_demo", _fake_configure)
    out = await _tools()["fleet_demo_configure"].ainvoke(
        {"slug": "demo-x", "model_connection": "anthropic", "model": "claude", "plugin_settings": {"notes": {"max": 5}}}
    )
    assert out["ok"] is True
    assert out["changed"] == ["model", "plugins"]
    # Blank strings are normalized to None so the core's "both required" rule reads cleanly.
    assert seen["model_connection"] == "anthropic" and seen["model"] == "claude"


async def test_list_returns_only_demo_members(monkeypatch):
    rows = [{"id": "demo-x", "name": "demo-x", "port": 7872, "demo": True, "created_by": "pm", "created_at": "now"}]
    monkeypatch.setattr(demo, "list_demos", lambda: rows)
    out = await _tools()["fleet_demo_list"].ainvoke({})
    assert out == rows


@pytest.mark.parametrize("alias", ["Demo display name", "remote-member-id"])
async def test_chat_routes_checked_record_id_not_display_name(monkeypatch, alias):
    checked = []

    def require_demo(slug):
        checked.append(slug)
        return {"id": "demo-canonical", "name": alias, "demo": True}

    monkeypatch.setattr(demo, "require_demo", require_demo)
    requests = []
    _install_http(monkeypatch, body={"response": "demo reply"}, capture=requests)
    out = await _tools()["fleet_demo_chat"].ainvoke({"slug": alias, "message": "hello"})
    assert checked == [alias]
    assert out["reply"] == "demo reply"
    assert requests[0].url.path == "/agents/demo-canonical/api/chat"


async def test_chat_refuses_record_without_id(monkeypatch):
    monkeypatch.setattr(demo, "require_demo", lambda slug: {"demo": True})
    _forbid_http(monkeypatch)
    out = await _tools()["fleet_demo_chat"].ainvoke({"slug": "demo-x", "message": "hi"})
    assert out == {"ok": False, "refused": fdt._refusal("demo-x")}


@pytest.mark.parametrize(
    ("operation", "arguments", "result"),
    [
        ("create", {"archetype": "basic", "model_connection": "local", "model": "test"}, {}),
        ("configure", {"slug": "demo-x"}, {}),
        ("delete", {"slug": "demo-x"}, None),
        ("list", {}, []),
    ],
)
async def test_blocking_operations_leave_event_loop_responsive(monkeypatch, operation, arguments, result):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()

    def require_demo(slug):
        assert threading.get_ident() != loop_thread
        return {"id": slug, "demo": True}

    def blocking_op(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "event loop did not release the blocking operation"
        return result

    monkeypatch.setattr(demo, "require_demo", require_demo)
    monkeypatch.setattr(demo, f"{operation}_demo" if operation != "list" else "list_demos", blocking_op)
    task = asyncio.create_task(_tools()[f"fleet_demo_{operation}"].ainvoke(arguments))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert not task.done()
        _install_http(monkeypatch, body={"response": "still responsive"})
        chat = await asyncio.wait_for(
            _tools()["fleet_demo_chat"].ainvoke({"slug": "demo-x", "message": "hi"}), timeout=2
        )
        assert chat["reply"] == "still responsive"
    finally:
        release.set()
        await task


@pytest.mark.parametrize("operation", ["create", "configure", "delete"])
@pytest.mark.parametrize("failure", ["workspace", "supervisor", "io", "unexpected"])
async def test_operation_failures_are_structured_and_do_not_leak(monkeypatch, operation, failure):
    from graph.fleet.supervisor import FleetError
    from graph.workspaces.manager import WorkspaceError

    errors = {"workspace": WorkspaceError, "supervisor": FleetError, "io": OSError, "unexpected": RuntimeError}
    calls = []
    monkeypatch.setattr(demo, "require_demo", lambda slug: {"id": slug, "demo": True})

    def fail(*args, **kwargs):
        calls.append(operation)
        raise errors[failure]("SECRET-SCOPED-BEARER")

    monkeypatch.setattr(demo, f"{operation}_demo", fail)
    arguments = {"archetype": "basic", "model_connection": "local", "model": "test"} if operation == "create" else {"slug": "demo-x"}
    out = await _tools()[f"fleet_demo_{operation}"].ainvoke(arguments)
    assert out["ok"] is False
    assert out["error"] == "operation_failed"
    assert errors[failure].__name__ in out["detail"]
    assert "SECRET-SCOPED-BEARER" not in json.dumps(out)
    assert calls == [operation]  # No automatic retry of potentially partial mutations.


@pytest.mark.parametrize("operation", ["configure", "delete"])
async def test_member_reaped_after_initial_guard_returns_refusal(monkeypatch, operation):
    checks = []

    def require_demo(slug):
        checks.append(slug)
        if len(checks) > 1:
            raise demo.NotDemoMember("member was reaped")
        return {"id": slug, "demo": True}

    monkeypatch.setattr(demo, "require_demo", require_demo)
    # Use the real core operation: its second stamp check must refuse before mutation.
    out = await _tools()[f"fleet_demo_{operation}"].ainvoke({"slug": "demo-x"})
    assert checks == ["demo-x", "demo-x"]
    assert out == {"ok": False, "refused": fdt._refusal("demo-x")}


async def test_delete_busy_workspace_reports_retryable_partial_outcome(monkeypatch):
    from graph.workspaces.manager import WorkspaceBusy
    from ops import fleet

    monkeypatch.setattr(demo, "require_demo", lambda slug: {"id": slug, "demo": True})

    def remove_sync(slug, *, purge):
        assert slug == "demo-x" and purge is True
        raise WorkspaceBusy("locked workspace")

    monkeypatch.setattr(fleet, "remove_sync", remove_sync)
    out = await _tools()["fleet_demo_delete"].ainvoke({"slug": "demo-x"})
    assert out["ok"] is False
    assert out["error"] == "workspace_busy"
    assert out["retryable"] is True
    assert "incomplete" in out["detail"]
    assert "deleted" not in out


@pytest.mark.parametrize("operation", ["configure", "chat", "delete"])
async def test_guard_io_failure_returns_error_without_mutation(monkeypatch, operation):
    from unittest.mock import AsyncMock

    def fail(slug):
        raise OSError("registry unreadable SECRET-SCOPED-BEARER")

    monkeypatch.setattr(demo, "require_demo", fail)
    post_chat = AsyncMock()
    monkeypatch.setattr(fdt, "_post_chat", post_chat)
    arguments = {"slug": "demo-x"}
    if operation == "chat":
        arguments["message"] = "hi"
    else:
        monkeypatch.setattr(demo, f"{operation}_demo", _boom_op(operation))
    out = await _tools()[f"fleet_demo_{operation}"].ainvoke(arguments)
    assert out["ok"] is False
    assert out["error"] == "operation_failed"
    assert "OSError" in out["detail"]
    assert "SECRET-SCOPED-BEARER" not in json.dumps(out)
    post_chat.assert_not_called()


@pytest.mark.parametrize("error", [OSError, RuntimeError])
async def test_list_failure_returns_structured_error_without_leaking(monkeypatch, error):
    def fail():
        raise error("registry unreadable SECRET-SCOPED-BEARER")

    monkeypatch.setattr(demo, "list_demos", fail)
    out = await _tools()["fleet_demo_list"].ainvoke({})
    assert out["ok"] is False
    assert out["error"] == "operation_failed"
    assert error.__name__ in out["detail"]
    assert "SECRET-SCOPED-BEARER" not in json.dumps(out)


async def test_chat_setup_failure_returns_structured_error_without_leaking(monkeypatch):
    monkeypatch.setattr(demo, "require_demo", lambda slug: {"id": slug, "demo": True})
    _install_http(monkeypatch)

    def fail():
        raise OSError("SECRET-SCOPED-BEARER")

    monkeypatch.setattr(fdt, "_make_client", fail)
    out = await _tools()["fleet_demo_chat"].ainvoke({"slug": "demo-x", "message": "hi"})
    assert out["ok"] is False
    assert out["error"] == "operation_failed"
    assert "SECRET-SCOPED-BEARER" not in json.dumps(out)
