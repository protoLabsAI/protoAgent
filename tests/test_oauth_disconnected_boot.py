"""A signed-out native OAuth provider must not make the server unbootable (#2458).

The v0.130.0 Windows acceptance run hit the dead end: disconnect Codex, restart,
and ``resolve_codex_oauth()`` raises ``OAuthCredentialError`` inside startup graph
construction — the backend exits before binding its port, so the reconnect routes
the user now needs are unreachable. Signed-out is an *intentional* state; these
tests pin the recovery contract:

  boot with the marker → graphless server (no exception, reason recorded) →
  in-console sign-in → reload → live graph again. No process restart anywhere.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import time

import pytest

from runtime.state import STATE, AppState


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.sig"


@pytest.fixture
def state_guard():
    """Snapshot/restore every AppState field — ``_init_langgraph_agent`` mutates the
    process-wide singleton, and a leaked ``STATE.graph`` would haunt later tests."""
    saved = {f.name: getattr(STATE, f.name) for f in dataclasses.fields(AppState)}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(STATE, name, value)


@pytest.fixture
def disconnected_codex_instance(tmp_path, monkeypatch):
    """A tmp instance: setup complete, provider openai-codex, disconnect marker
    present, no credential anywhere (the CLI bootstrap file is pointed at nothing
    so the dev machine's real ~/.codex can't leak in)."""
    from graph.providers import oauth as oauth_mod
    from infra.paths import instance_paths

    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "no-cli-auth.json")

    ip = instance_paths()
    ip.config_dir.mkdir(parents=True, exist_ok=True)
    (ip.config_dir / "langgraph-config.yaml").write_text(
        "model:\n  provider: openai-codex\n  name: gpt-5-codex\n",
        encoding="utf-8",
    )
    (ip.config_dir / ".setup-complete").write_text("", encoding="utf-8")
    (ip.config_dir / "oauth-disconnected.json").write_text('["openai-codex"]', encoding="utf-8")
    return ip


def test_boot_survives_disconnect_marker(disconnected_codex_instance, state_guard):
    """The core #2458 contract: the marker present at boot yields a graphless
    server with the reason recorded — not a dead process."""
    from server.agent_init import _init_langgraph_agent

    _init_langgraph_agent()  # must not raise

    assert STATE.graph is None
    err = STATE.graph_auth_error
    assert err and err["provider"] == "openai-codex"
    assert "disconnected" in err["message"].lower()
    assert err["relogin"] is True
    # Graph-independent machinery still built — reconnect's reload only rebuilds
    # the graph, so anything missing here would stay dead until a full restart.
    assert STATE.watch_controller is not None
    # The cache warmer pings the provider; while signed out it must not exist.
    assert STATE.cache_warmer is None


def test_reconnect_then_reload_restores_graph(disconnected_codex_instance, state_guard):
    """disconnect → boot graphless → sign in (store + marker cleared) → reload
    commits a live graph and clears the recorded auth error. No restart."""
    from graph.providers import oauth as oauth_mod
    from server.agent_init import _init_langgraph_agent, _reload_langgraph_agent

    _init_langgraph_agent()
    assert STATE.graph is None and STATE.graph_auth_error

    # What a completed in-console device sign-in does (oauth_login.codex_login_poll):
    # writes the instance store and clears the disconnect marker.
    fresh = _jwt({"exp": time.time() + 3600, "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}})
    (disconnected_codex_instance.config_dir / "codex-oauth.json").write_text(
        json.dumps({"tokens": {"access_token": fresh, "refresh_token": "r", "account_id": "acct-1"}}),
        encoding="utf-8",
    )
    oauth_mod.clear_disconnected("openai-codex", disconnected_codex_instance)

    ok, message = _reload_langgraph_agent()
    assert ok, f"reload failed after reconnect: {message}"
    assert STATE.graph is not None
    assert STATE.graph_auth_error is None


def test_chat_reports_signed_out_not_setup(state_guard):
    """With the auth error recorded, chat's graphless message must say signed
    out / reconnect — not point the user back at a wizard they already finished."""
    from server.chat import _setup_required_message

    STATE.graph = None
    STATE.graph_auth_error = {"provider": "openai-codex", "message": "Codex is disconnected in protoAgent. Sign in again to reconnect.", "relogin": True}
    [msg] = _setup_required_message()
    assert "Signed out" in msg["content"]
    assert "disconnected" in msg["content"]

    STATE.graph_auth_error = None
    [msg] = _setup_required_message()
    assert "Setup required" in msg["content"]


def test_runtime_status_carries_graph_auth_error():
    from operator_api.runtime import build_runtime_status

    err = {"provider": "openai-codex", "message": "disconnected", "relogin": True}
    out = build_runtime_status(config=None, setup_complete=True, graph_loaded=False, graph_auth_error=err)
    assert out["graph_auth_error"] == err
    out = build_runtime_status(config=object(), setup_complete=True, graph_loaded=True)
    assert out["graph_auth_error"] is None


def _disconnect_client(monkeypatch, *, marker_present: bool = True):
    """TestClient + a fake ``disconnect()`` that reports success without network.

    ``marker_present`` fakes the post-await storage truth the route re-checks:
    True = the disconnect marker survived (normal case); False = a concurrent
    sign-in completed during the await and cleared it (the TOCTOU race)."""
    import types

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.config_routes import register_config_routes

    monkeypatch.setattr(
        "graph.providers.oauth.disconnect",
        lambda provider: types.SimpleNamespace(
            as_dict=lambda: {"provider": provider, "removed": True, "revoked": True, "note": "test"}
        ),
    )
    monkeypatch.setattr(
        "graph.providers.oauth.is_disconnected", lambda provider, paths=None: marker_present
    )
    app = FastAPI()
    register_config_routes(app)
    return TestClient(app)


def test_disconnect_unloads_live_graph(state_guard, monkeypatch):
    """#2459: disconnecting the provider the live graph runs on must unload the
    graph (the in-memory client holds the just-revoked token) and land in the
    same signed-out state a disconnected boot produces."""
    import types

    client = _disconnect_client(monkeypatch)
    STATE.graph = object()
    STATE.graph_config = types.SimpleNamespace(model_provider="openai-codex")

    stops = []

    class _Warmer:
        async def stop(self):
            stops.append(1)

    STATE.cache_warmer = _Warmer()

    res = client.post("/api/config/oauth/disconnect", json={"provider": "openai-codex"}).json()
    assert res["graph_unloaded"] is True
    assert STATE.graph is None
    assert STATE.cache_warmer is None
    assert stops == [1]
    err = STATE.graph_auth_error
    assert err and err["provider"] == "openai-codex" and err["relogin"] is True


def test_disconnect_rejects_empty_provider(state_guard, monkeypatch):
    client = _disconnect_client(monkeypatch)
    assert client.post("/api/config/oauth/disconnect", json={}).status_code == 400


def test_disconnect_race_with_completed_signin_keeps_graph(state_guard, monkeypatch):
    """TOCTOU (QA review on #2476): a sign-in that completes during disconnect's
    await clears the marker and rebuilds the graph — the route's post-await
    re-check must let that work stand instead of clobbering it."""
    import types

    client = _disconnect_client(monkeypatch, marker_present=False)
    rebuilt_graph = object()
    STATE.graph = rebuilt_graph
    STATE.graph_config = types.SimpleNamespace(model_provider="openai-codex")
    STATE.graph_auth_error = None
    warmer = object()
    STATE.cache_warmer = warmer

    res = client.post("/api/config/oauth/disconnect", json={"provider": "openai-codex"}).json()
    assert res["graph_unloaded"] is False
    assert STATE.graph is rebuilt_graph
    assert STATE.cache_warmer is warmer
    assert STATE.graph_auth_error is None


def test_disconnect_of_other_provider_keeps_graph(state_guard, monkeypatch):
    """Disconnecting a provider the live graph does NOT run on is storage-only."""
    import types

    client = _disconnect_client(monkeypatch)
    live_graph = object()
    STATE.graph = live_graph
    STATE.graph_config = types.SimpleNamespace(model_provider="openai-codex")
    STATE.graph_auth_error = None

    res = client.post("/api/config/oauth/disconnect", json={"provider": "anthropic-oauth"}).json()
    assert "graph_unloaded" not in res
    assert STATE.graph is live_graph
    assert STATE.graph_auth_error is None


def test_oauth_complete_route_triggers_graph_rebuild(state_guard, monkeypatch):
    """A completed sign-in on a graphless, setup-complete server reloads the
    graph inline — the route response says whether chat is back."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.config_routes import register_config_routes

    app = FastAPI()
    register_config_routes(app)
    client = TestClient(app)

    STATE.graph = None
    monkeypatch.setattr("graph.config_io.is_setup_complete", lambda: True)
    monkeypatch.setattr(
        "graph.providers.oauth_login.codex_login_poll", lambda flow_id: {"status": "complete"}
    )
    reloads = []

    def _fake_reload():
        reloads.append(1)
        STATE.graph = object()
        return True, "ok"

    monkeypatch.setattr("server.agent_init._reload_langgraph_agent", _fake_reload)

    res = client.post("/api/config/oauth/poll", json={"provider": "openai-codex", "flow_id": "f1"}).json()
    assert res["status"] == "complete"
    assert res["graph_reloaded"] is True
    assert len(reloads) == 1

    # A still-pending poll must not reload anything.
    monkeypatch.setattr(
        "graph.providers.oauth_login.codex_login_poll", lambda flow_id: {"status": "pending"}
    )
    res = client.post("/api/config/oauth/poll", json={"provider": "openai-codex", "flow_id": "f1"}).json()
    assert res["status"] == "pending"
    assert "graph_reloaded" not in res
    assert len(reloads) == 1

    # A live graph (normal wizard sign-in) skips the rebuild too.
    monkeypatch.setattr(
        "graph.providers.oauth_login.codex_login_poll", lambda flow_id: {"status": "complete"}
    )
    res = client.post("/api/config/oauth/poll", json={"provider": "openai-codex", "flow_id": "f1"}).json()
    assert res["status"] == "complete"
    assert "graph_reloaded" not in res
    assert len(reloads) == 1


def test_signin_completes_a_pending_provider_switch(state_guard, monkeypatch):
    """A live provider switch persists YAML first, reloads second — with no
    credential the reload fails and saved ≠ live. The sign-in that follows must
    finish the switch inline (reload), not leave the user to re-save."""
    import types

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.config_routes import register_config_routes

    app = FastAPI()
    register_config_routes(app)
    client = TestClient(app)

    STATE.graph = object()  # live graph on the OLD provider
    STATE.graph_config = types.SimpleNamespace(model_provider="openai")
    monkeypatch.setattr("graph.config_io.is_setup_complete", lambda: True)
    monkeypatch.setattr(
        "graph.config.LangGraphConfig.from_yaml",
        classmethod(lambda cls, path: types.SimpleNamespace(model_provider="openai-codex")),
    )
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: "unused")
    monkeypatch.setattr(
        "graph.providers.oauth_login.codex_login_poll", lambda flow_id: {"status": "complete"}
    )
    reloads = []
    monkeypatch.setattr(
        "server.agent_init._reload_langgraph_agent", lambda: (reloads.append(1), (True, "ok"))[1]
    )

    res = client.post("/api/config/oauth/poll", json={"provider": "openai-codex", "flow_id": "f"}).json()
    assert res["graph_reloaded"] is True and reloads == [1]

    # Saved == live (no pending switch) → completed sign-in does NOT reload.
    monkeypatch.setattr(
        "graph.config.LangGraphConfig.from_yaml",
        classmethod(lambda cls, path: types.SimpleNamespace(model_provider="openai")),
    )
    res = client.post("/api/config/oauth/poll", json={"provider": "openai-codex", "flow_id": "f"}).json()
    assert "graph_reloaded" not in res and reloads == [1]
