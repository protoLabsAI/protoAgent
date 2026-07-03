"""The hot-reload graph rebuild must thread the SAME runtime deps as boot.

`_reload_langgraph_agent` (drawer saves, /api/settings, /api/config/reload)
rebuilds the compiled graph from fresh config. Stores that survive reloads
(checkpointer, tasks store, background manager) must be re-threaded into
`create_agent_graph` — the tasks store was silently omitted, so ANY settings
hot-reload dropped task_create/task_list/task_update/task_close from the
rebuilt graph until the next full restart (found via the Tools-tab row
toggles, which hot-reload on every flip and made the 4 rows vanish).

The test intercepts `create_agent_graph` inside a real `_reload_langgraph_agent`
run (heavy builders stubbed), captures its kwargs, and RAISES — the reload's
own failure path then returns without committing anything to STATE, so the
process-global state is untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _reset_denylist():
    """The reload syncs the module-global denylist from the temp config; clear it."""
    from tools.lg_tools import set_disabled_tools

    yield
    set_disabled_tools([])


def test_reload_threads_surviving_stores_into_the_rebuilt_graph(tmp_path, monkeypatch):
    import graph.config_io as cio
    import server.agent_init as ai
    from runtime.state import STATE

    # A minimal live leaf (scheduler off so the reload doesn't construct one).
    leaf = tmp_path / "langgraph-config.yaml"
    leaf.write_text("scheduler:\n  enabled: false\n")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "ensure_live_config", lambda: None)
    monkeypatch.setattr(cio, "is_setup_complete", lambda: True)

    # Stub the heavy builders — this test is about the create_agent_graph WIRING.
    monkeypatch.setattr(ai, "_build_knowledge_store", lambda cfg: None)
    monkeypatch.setattr(ai, "_build_mcp", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(ai, "_apply_plugin_knowledge_backend", lambda cfg, store, plugins: store)
    monkeypatch.setattr(ai, "_register_plugin_subagents", lambda subagents: None)
    monkeypatch.setattr(ai, "_apply_config_subagents", lambda cfg: None)
    monkeypatch.setattr(ai, "_resolve_plugin_middleware", lambda cfg, mw: [])
    monkeypatch.setattr(ai, "_build_skills_index", lambda cfg, extra_skill_dirs=None: None)
    monkeypatch.setattr(ai, "_build_inbox_store", lambda cfg: None)
    monkeypatch.setattr(
        ai,
        "_build_plugins",
        lambda *a, **k: SimpleNamespace(
            mcp_servers=[], tools=[], tool_plugins={}, skill_dirs=[], meta=[],
            chat_commands={}, subagents=[], middleware=[], late_tool_factories=[], routers=[],
        ),
    )

    # The reload-surviving stores (monkeypatch snapshots + restores the real values).
    checkpointer, tasks_store, background_mgr = object(), object(), object()
    monkeypatch.setattr(STATE, "checkpointer", checkpointer, raising=False)
    monkeypatch.setattr(STATE, "tasks_store", tasks_store, raising=False)
    monkeypatch.setattr(STATE, "background_mgr", background_mgr, raising=False)
    monkeypatch.setattr(STATE, "scheduler", None, raising=False)
    monkeypatch.setattr(STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(STATE, "workflow_run", None, raising=False)

    # Capture the rebuild call, then abort so nothing commits to STATE.
    import graph.agent as ga

    captured: dict = {}

    def _capture(config, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop before commit")

    monkeypatch.setattr(ga, "create_agent_graph", _capture)

    ok, msg = ai._reload_langgraph_agent()

    assert ok is False and "rebuild failed" in msg  # our abort — nothing committed
    assert captured, "create_agent_graph was never reached"
    assert captured["checkpointer"] is checkpointer
    assert captured["background_mgr"] is background_mgr
    # THE regression: the tasks store must ride the rebuild like its siblings.
    assert captured["tasks_store"] is tasks_store


def test_reload_refreshes_plugin_verifier_registry(tmp_path, monkeypatch):
    """#1752: a committed hot-reload must push the rebuilt plugin verifiers into the LIVE
    registry the watch/goal controllers consult — else a plugin update that ships a new
    verifier leaves it 'unknown plugin verifier' (armed but blind) until a full restart."""
    import graph.agent as ga
    import graph.config_io as cio
    import server.agent_init as ai
    from graph.goals import verifiers as gv
    from runtime.state import STATE

    leaf = tmp_path / "langgraph-config.yaml"
    leaf.write_text("scheduler:\n  enabled: false\n")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "ensure_live_config", lambda: None)
    monkeypatch.setattr(cio, "is_setup_complete", lambda: True)

    # Heavy builders + commit-side effects stubbed — this test is about the registry refresh.
    monkeypatch.setattr(ai, "_build_knowledge_store", lambda cfg: None)
    monkeypatch.setattr(ai, "_build_mcp", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(ai, "_apply_plugin_knowledge_backend", lambda cfg, store, plugins: store)
    monkeypatch.setattr(ai, "_register_plugin_subagents", lambda subagents: None)
    monkeypatch.setattr(ai, "_apply_config_subagents", lambda cfg: None)
    monkeypatch.setattr(ai, "_resolve_plugin_middleware", lambda cfg, mw: [])
    monkeypatch.setattr(ai, "_build_skills_index", lambda cfg, extra_skill_dirs=None: None)
    monkeypatch.setattr(ai, "_build_inbox_store", lambda cfg: None)
    monkeypatch.setattr(ai, "_mount_plugin_routers", lambda routers: None)
    monkeypatch.setattr(ai, "_reload_plugin_surfaces", lambda cfg: None)
    # The commit block also live-reloads egress/callback/bearer globals — no-op them so this
    # test can't leak allowlist/token state into others.
    import security.egress as _egress
    import security.policy as _policy

    monkeypatch.setattr(_egress, "set_allowed_hosts", lambda *a, **k: None)
    monkeypatch.setattr(_policy, "set_callback_allowlist", lambda *a, **k: None)
    try:
        import a2a_impl.auth as _a2a_auth

        monkeypatch.setattr(_a2a_auth, "set_bearer_token", lambda *a, **k: None)
    except ImportError:
        pass

    async def _new_verifier(spec, ctx):
        from graph.goals.types import VerifyResult

        return VerifyResult(True, "ok", "")

    monkeypatch.setattr(
        ai,
        "_build_plugins",
        lambda *a, **k: SimpleNamespace(
            mcp_servers=[], tools=[], tool_plugins={}, skill_dirs=[], meta=[],
            chat_commands={}, subagents=[], middleware=[], late_tool_factories=[], routers=[],
            goal_verifiers={"demo:brand_new": _new_verifier}, goal_hooks=[], watch_hooks=[],
        ),
    )
    # Let the graph rebuild SUCCEED so the commit block (with the registry refresh) runs.
    monkeypatch.setattr(ga, "create_agent_graph", lambda config, **kwargs: object())

    # Surviving stores + scheduler/graph off.
    for name in ("checkpointer", "tasks_store", "background_mgr"):
        monkeypatch.setattr(STATE, name, object(), raising=False)
    for name in ("scheduler", "graph", "workflow_registry", "workflow_run"):
        monkeypatch.setattr(STATE, name, None, raising=False)

    # Pre-state: an OLD mapping without the new verifier (mirrors the pre-update registry).
    gv.set_plugin_verifiers({"demo:old": _new_verifier})
    assert "demo:brand_new" not in gv.plugin_verifier_names()
    try:
        ok, msg = ai._reload_langgraph_agent()
        assert ok is True, msg
        # THE regression: the reload pushed the rebuilt verifiers into the live registry.
        assert "demo:brand_new" in gv.plugin_verifier_names()
    finally:
        gv.set_plugin_verifiers({})
