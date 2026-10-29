"""Boot path of "Explore without a model" (#4566).

A visitor finishes setup with no plan, key or local model: the setup marker is present but
the config names no usable model. The server's NEXT boot (``_init_langgraph_agent``) must
stay up and serve the console rather than crash building a client with no key — the same
contract ``_reload_langgraph_agent`` already honours after the finish. These pin the boot
half: the gate skips the graph build (graphless, no auth error), the graph-independent
machinery still builds, and the moment a model is saved a reload compiles with no restart.
"""

from __future__ import annotations

import dataclasses

import pytest

from runtime.state import STATE, AppState


@pytest.fixture
def state_guard():
    """Snapshot/restore every AppState field — ``_init_langgraph_agent`` mutates the
    process-wide singleton, and a leaked ``STATE.graph`` would haunt later tests (same
    guard as tests/test_oauth_disconnected_boot.py)."""
    saved = {f.name: getattr(STATE, f.name) for f in dataclasses.fields(AppState)}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(STATE, name, value)


@pytest.fixture
def explore_mode_instance(tmp_path, monkeypatch):
    """A tmp instance: setup complete, but no model — no connection declared and a blank
    ``model.name``. OPENAI_API_KEY is cleared so the conftest default key can't leak in and
    read the default route as configured."""
    from infra.paths import instance_paths

    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    ip = instance_paths()
    ip.config_dir.mkdir(parents=True, exist_ok=True)
    (ip.config_dir / "langgraph-config.yaml").write_text(
        "providers: []\nmodel:\n  name: ''\nidentity:\n  name: protoagent\n",
        encoding="utf-8",
    )
    (ip.config_dir / ".setup-complete").write_text("", encoding="utf-8")
    return ip


def test_explore_mode_config_reads_as_unconfigured(explore_mode_instance):
    """Sanity: the written config is genuinely explore mode — the gate's predicate is
    False, which is why the boot below skips the graph build."""
    from graph.config import LangGraphConfig, model_is_configured
    from graph.config_io import config_yaml_path

    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    assert cfg.providers_declared is True and not cfg.providers
    assert model_is_configured(cfg) is False


def test_boot_without_a_model_stays_up_graphless(explore_mode_instance, state_guard):
    """The #4566 boot contract: setup complete + no model → a graphless server, no raise,
    and NOT recorded as a signed-out auth error (explore mode is not a disconnect)."""
    from server.agent_init import _init_langgraph_agent

    _init_langgraph_agent()  # must not raise even with no model

    assert STATE.graph is None  # graph build skipped — no model to build against
    assert STATE.graph_auth_error is None  # explore mode, not an OAuth sign-out
    # Graph-independent machinery still built — a later model save reloads only the graph
    # (reconfiguring, not rebuilding, these), so anything skipped here would stay dead
    # until a full restart.
    assert STATE.watch_controller is not None
    # No model means nothing to warm.
    assert STATE.cache_warmer is None


def test_saving_a_model_reloads_into_a_live_graph(explore_mode_instance, state_guard):
    """Explore → connect a model: a reload after writing a usable key compiles the graph
    with NO restart (the console flips from "connect a model" to a live chat)."""
    from server.agent_init import _init_langgraph_agent, _reload_langgraph_agent

    _init_langgraph_agent()
    assert STATE.graph is None

    # What saving a model in Settings ▸ Model writes: a default-route key (no network at
    # build time — ChatOpenAI is lazy), so the reload gate's predicate flips to True.
    explore_mode_instance_config = explore_mode_instance.config_dir / "langgraph-config.yaml"
    explore_mode_instance_config.write_text(
        "model:\n  name: gpt-x\n  api_base: http://gw/v1\n  api_key: sk-live\nidentity:\n  name: protoagent\n",
        encoding="utf-8",
    )

    ok, message = _reload_langgraph_agent()
    assert ok, f"reload failed after saving a model: {message}"
    assert STATE.graph is not None
    assert STATE.graph_auth_error is None
