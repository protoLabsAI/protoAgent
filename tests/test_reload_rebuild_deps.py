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
            mcp_servers=[],
            tools=[],
            tool_plugins={},
            skill_dirs=[],
            meta=[],
            surfaces=[],
            chat_commands={},
            subagents=[],
            middleware=[],
            late_tool_factories=[],
            routers=[],
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
    """A committed hot-reload must re-publish the boot-time plugin wiring that lives
    outside the rebuilt graph:

    - #1752: the plugin verifiers pushed into the LIVE registry the watch/goal
      controllers consult — else a plugin update that ships a new verifier leaves it
      'unknown plugin verifier' (armed but blind) until a full restart.
    - the STATE-read seams a hot enable/update also left stale: the thread_id resolver,
      the A2A card skills, and the workflow recipe dirs. Each is read fresh from STATE by
      its consumer, so re-publishing on reload is what makes a hot enable take effect
      without a restart."""
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
        monkeypatch.setattr(_a2a_auth, "set_federation_token", lambda *a, **k: None)
        monkeypatch.setattr(_a2a_auth, "set_public_prefixes", lambda *a, **k: None)
    except ImportError:
        pass

    async def _new_verifier(spec, ctx):
        from graph.goals.types import VerifyResult

        return VerifyResult(True, "ok", "")

    def _new_resolver(request_metadata, session_id):
        return f"reloaded:{session_id}"

    new_a2a_skills = [{"id": "demo-skill", "name": "Demo"}]
    new_workflow_dirs = ["/plugins/demo/workflows"]

    monkeypatch.setattr(
        ai,
        "_build_plugins",
        lambda *a, **k: SimpleNamespace(
            mcp_servers=[],
            tools=[],
            tool_plugins={},
            skill_dirs=[],
            meta=[],
            chat_commands={},
            subagents=[],
            middleware=[],
            late_tool_factories=[],
            routers=[],
            public_paths=[],
            federation_paths=[],
            goal_verifiers={"demo:brand_new": _new_verifier},
            goal_hooks=[],
            watch_hooks=[],
            lifecycle_hooks=[],
            surfaces=[],
            thread_id_resolver=_new_resolver,
            a2a_skills=new_a2a_skills,
            workflow_dirs=new_workflow_dirs,
        ),
    )
    # Let the graph rebuild SUCCEED so the commit block (with the registry refresh) runs.
    monkeypatch.setattr(ga, "create_agent_graph", lambda config, **kwargs: object())

    # Surviving stores + scheduler/graph off.
    for name in ("checkpointer", "tasks_store", "background_mgr"):
        monkeypatch.setattr(STATE, name, object(), raising=False)
    for name in ("scheduler", "graph", "workflow_registry", "workflow_run"):
        monkeypatch.setattr(STATE, name, None, raising=False)

    # Pre-state: OLD/stale values (mirrors the pre-update process) for every seam.
    gv.set_plugin_verifiers({"demo:old": _new_verifier})
    assert "demo:brand_new" not in gv.plugin_verifier_names()
    monkeypatch.setattr(STATE, "thread_id_resolver", None, raising=False)
    monkeypatch.setattr(STATE, "plugin_a2a_skills", [], raising=False)
    monkeypatch.setattr(STATE, "plugin_workflow_dirs", [], raising=False)
    try:
        ok, msg = ai._reload_langgraph_agent()
        assert ok is True, msg
        # THE regression: the reload pushed the rebuilt verifiers into the live registry.
        assert "demo:brand_new" in gv.plugin_verifier_names()
        # …and re-published the STATE-read seams that were boot-only before this fix.
        assert STATE.thread_id_resolver is _new_resolver
        assert STATE.plugin_a2a_skills == new_a2a_skills
        assert STATE.plugin_workflow_dirs == new_workflow_dirs
    finally:
        gv.set_plugin_verifiers({})


def _stub_reload_for_auth_capture(tmp_path, monkeypatch):
    """Common heavy-builder stubbing so `_reload_langgraph_agent()` reaches its commit
    block, shared by the two auth-token-reload tests below. Writes a minimal live leaf
    config and returns `(leaf_path, calls)` — `calls` records every `(kind, token)`
    set_bearer_token/set_federation_token call the reload makes. Callers that need a
    secrets.yaml sibling write it under `leaf_path.parent` before calling
    `ai._reload_langgraph_agent()`."""
    import graph.config_io as cio
    import server.agent_init as ai
    from runtime.state import STATE

    leaf = tmp_path / "langgraph-config.yaml"
    leaf.write_text("scheduler:\n  enabled: false\n")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "ensure_live_config", lambda: None)
    monkeypatch.setattr(cio, "is_setup_complete", lambda: True)

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
    monkeypatch.setattr(
        ai,
        "_build_plugins",
        lambda *a, **k: SimpleNamespace(
            mcp_servers=[],
            tools=[],
            tool_plugins={},
            skill_dirs=[],
            meta=[],
            chat_commands={},
            subagents=[],
            middleware=[],
            late_tool_factories=[],
            routers=[],
            public_paths=[],
            federation_paths=[],
            goal_verifiers={},
            goal_hooks=[],
            watch_hooks=[],
            lifecycle_hooks=[],
            surfaces=[],
            thread_id_resolver=None,
            a2a_skills=[],
            workflow_dirs=[],
        ),
    )
    import graph.agent as ga

    monkeypatch.setattr(ga, "create_agent_graph", lambda config, **kwargs: object())

    import security.egress as _egress
    import security.policy as _policy

    monkeypatch.setattr(_egress, "set_allowed_hosts", lambda *a, **k: None)
    monkeypatch.setattr(_policy, "set_callback_allowlist", lambda *a, **k: None)

    for name in ("checkpointer", "tasks_store", "background_mgr"):
        monkeypatch.setattr(STATE, name, object(), raising=False)
    for name in ("scheduler", "graph", "workflow_registry", "workflow_run"):
        monkeypatch.setattr(STATE, name, None, raising=False)

    import a2a_impl.auth as _a2a_auth

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(_a2a_auth, "set_bearer_token", lambda t: calls.append(("bearer", t)))
    monkeypatch.setattr(_a2a_auth, "set_federation_token", lambda t: calls.append(("federation", t)))
    monkeypatch.setattr(_a2a_auth, "set_public_prefixes", lambda *a, **k: None)
    return leaf, calls


def test_reload_rotates_the_federation_token_live(tmp_path, monkeypatch):
    """A Settings-drawer save of auth.federation_token must take effect without a
    restart, same as auth.token already does (#1504). Before this fix, the reload
    commit block called set_bearer_token but never set_federation_token — a
    federation-token edit would persist to secrets.yaml but silently keep using
    whatever token (or none) was active at boot until the next full restart."""
    import graph.config_io as cio
    import server.agent_init as ai

    leaf, calls = _stub_reload_for_auth_capture(tmp_path, monkeypatch)
    secrets_path = leaf.parent / "secrets.yaml"
    secrets_path.write_text("auth:\n  token: new-operator-token\n  federation_token: new-federation-token\n")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: secrets_path)

    ok, msg = ai._reload_langgraph_agent()

    assert ok is True, msg
    assert ("bearer", "new-operator-token") in calls
    # THE regression: federation_token must rotate live exactly like the bearer token.
    assert ("federation", "new-federation-token") in calls


def test_reload_does_not_wipe_an_env_only_federation_token(tmp_path, monkeypatch):
    """A deployment configured purely via A2A_FEDERATION_TOKEN/A2A_AUTH_TOKEN (no
    secrets.yaml/YAML value — the documented env-only path, `docs/reference/
    configuration.md`) must not have its live credential silently cleared by the
    FIRST Settings save of ANYTHING, auth-related or not. Before this fix, the reload
    passed the empty config value straight to set_bearer_token/set_federation_token
    with no env fallback at all.

    auth.configure()'s OWN contract distinguishes None ("unspecified, check env") from
    an explicit "" ("off, no fallback") — but boot's one real caller (server/__init__.py
    _main()) always collapses an empty config value to None before calling it
    (`(... or "") or None`), so boot's OBSERVED behavior is "falls back whenever the
    config value is empty" even though configure() itself can express the finer
    distinction for a caller that preserves it. The reload's new `or os.environ.get(...)`
    fallback matches that observed boot behavior, not configure()'s full three-state
    contract (#1504 review)."""
    import server.agent_init as ai

    _, calls = _stub_reload_for_auth_capture(tmp_path, monkeypatch)
    # No secrets.yaml at all — the config value resolves to "" for both fields.
    monkeypatch.setenv("A2A_AUTH_TOKEN", "env-operator-token")
    monkeypatch.setenv("A2A_FEDERATION_TOKEN", "env-federation-token")

    ok, msg = ai._reload_langgraph_agent()

    assert ok is True, msg
    assert ("bearer", "env-operator-token") in calls
    assert ("federation", "env-federation-token") in calls


def test_pre_setup_reload_does_not_crash_on_the_surface_publish(tmp_path, monkeypatch):
    """A reload while setup is still PENDING must not 500.

    The setup-pending branch skips the whole plugin build, so `new_plugins` is never bound —
    but the commit published `STATE.plugin_surfaces = new_plugins.surfaces` unconditionally
    and raised UnboundLocalError. Every pre-setup reload 500'd: installing a plugin during
    the wizard (its auto-enable reloads through here), or saving any setting before setup is
    marked complete. This is the exact trap the sibling `new_middleware = []` was added to
    close — the surface publish landed after it and missed the guard.
    """
    import graph.config_io as cio
    import server.agent_init as ai
    from runtime.state import STATE

    leaf = tmp_path / "langgraph-config.yaml"
    leaf.write_text("scheduler:\n  enabled: false\n")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "ensure_live_config", lambda: None)
    monkeypatch.setattr(cio, "is_setup_complete", lambda: False)  # ← the wizard is still up
    monkeypatch.setattr(ai, "_reload_plugin_surfaces", lambda cfg: None)
    import security.egress as _egress
    import security.policy as _policy

    monkeypatch.setattr(_egress, "set_allowed_hosts", lambda *a, **k: None)
    monkeypatch.setattr(_policy, "set_callback_allowlist", lambda *a, **k: None)
    monkeypatch.setattr(STATE, "scheduler", None, raising=False)
    monkeypatch.setattr(STATE, "graph", None, raising=False)
    monkeypatch.setattr(STATE, "plugin_surfaces", ["stale"], raising=False)

    ok, msg = ai._reload_langgraph_agent()

    assert ok is True and "setup not complete" in msg
    assert STATE.plugin_surfaces == []  # nothing wanted yet — the boot hook starts the real set


def test_reload_restamps_a_members_fleet_display_name(tmp_path, monkeypatch):
    """A fleet member's Settings ▸ Agent ▸ Identity save is proxied to the MEMBER, so it only
    ever wrote the member's own identity.name — while the console's agent switcher/header
    reads the HUB's fleet list, whose label comes from that workspace's `workspace.yaml`. The
    tab title and A2A card renamed; the dropdown kept the create-time name forever. The reload
    commit is the one choke point every identity change passes through, so it restamps there."""
    import yaml

    import graph.agent as ga
    import graph.config_io as cio
    import server.agent_init as ai
    from runtime.state import STATE

    # This process IS a workspace member: its instance root is a workspace dir with a record.
    ws = tmp_path / "scout-1a2b"
    (ws / "config").mkdir(parents=True)
    (ws / "workspace.yaml").write_text(yaml.safe_dump({"name": "scout", "id": "scout-1a2b", "port": 7871}))
    monkeypatch.setattr("infra.paths.instance_paths", lambda: type("P", (), {"instance_root": ws})())

    leaf = ws / "config" / "langgraph-config.yaml"
    leaf.write_text("scheduler:\n  enabled: false\nidentity:\n  name: ranger\n")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "ensure_live_config", lambda: None)
    monkeypatch.setattr(cio, "is_setup_complete", lambda: True)

    for name, stub in (
        ("_build_knowledge_store", lambda cfg: None),
        ("_apply_plugin_knowledge_backend", lambda cfg, store, plugins: store),
        ("_register_plugin_subagents", lambda subagents: None),
        ("_apply_config_subagents", lambda cfg: None),
        ("_resolve_plugin_middleware", lambda cfg, mw: []),
        ("_build_skills_index", lambda cfg, extra_skill_dirs=None: None),
        ("_build_inbox_store", lambda cfg: None),
        ("_mount_plugin_routers", lambda routers: None),
        ("_reload_plugin_surfaces", lambda cfg: None),
        ("_apply_plugin_registries", lambda plugins: None),
    ):
        monkeypatch.setattr(ai, name, stub)
    monkeypatch.setattr(ai, "_build_mcp", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(
        ai,
        "_build_plugins",
        lambda *a, **k: SimpleNamespace(
            mcp_servers=[],
            tools=[],
            tool_plugins={},
            skill_dirs=[],
            meta=[],
            chat_commands={},
            subagents=[],
            middleware=[],
            late_tool_factories=[],
            routers=[],
            public_paths=[],
            federation_paths=[],
            goal_verifiers={},
            goal_hooks=[],
            watch_hooks=[],
            lifecycle_hooks=[],
            surfaces=[],
            thread_id_resolver=None,
            a2a_skills=[],
            workflow_dirs=[],
        ),
    )
    import security.egress as _egress
    import security.policy as _policy

    monkeypatch.setattr(_egress, "set_allowed_hosts", lambda *a, **k: None)
    monkeypatch.setattr(_policy, "set_callback_allowlist", lambda *a, **k: None)
    monkeypatch.setattr(ga, "create_agent_graph", lambda config, **kwargs: object())
    for name in ("checkpointer", "tasks_store", "background_mgr"):
        monkeypatch.setattr(STATE, name, object(), raising=False)
    for name in ("scheduler", "graph", "workflow_registry", "workflow_run"):
        monkeypatch.setattr(STATE, name, None, raising=False)
    try:
        import a2a_impl.auth as _a2a_auth

        # This test's reload reaches the commit block (ok is True) — without these, it runs
        # the REAL setters and clears whatever bearer/federation token this process actually
        # has live (module-global state), leaking into whichever test runs next (#1504 review).
        monkeypatch.setattr(_a2a_auth, "set_bearer_token", lambda *a, **k: None)
        monkeypatch.setattr(_a2a_auth, "set_federation_token", lambda *a, **k: None)
        monkeypatch.setattr(_a2a_auth, "set_public_prefixes", lambda *a, **k: None)
    except ImportError:
        pass

    ok, msg = ai._reload_langgraph_agent()
    assert ok is True, msg
    # THE regression: the hub-visible display name now follows the member's own identity.
    assert yaml.safe_load((ws / "workspace.yaml").read_text())["name"] == "ranger"
    assert yaml.safe_load((ws / "workspace.yaml").read_text())["id"] == "scout-1a2b"  # id immutable
