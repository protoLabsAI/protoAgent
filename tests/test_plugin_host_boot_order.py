"""Cold-boot HOST ordering (the promptlab incident): a plugin that reads
``registry.host.config`` at register() time must see a LIVE getter on a fresh
boot — not the None it got when plugin loading ran before host wiring, which
made register-time captures silently skip (working on hot-enable, broken on
every app restart)."""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.plugins.host import HOST


def test_lazy_host_fields_are_wired_before_plugins_load(monkeypatch):
    import server.agent_init as agent_init
    from runtime.state import STATE

    monkeypatch.setattr(HOST, "config", None)
    monkeypatch.setattr(HOST, "apply_settings", None)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)

    seen: dict = {}

    def _fake_load_plugins(config, core_tool_names=None):
        # What a register() call observes at plugin-load time.
        seen["config_getter"] = HOST.config
        seen["apply"] = HOST.apply_settings

        class _R:  # minimal PluginLoadResult shape _build_plugins reads
            meta: list = []
            tools: list = []
            skill_dirs: list = []
            workflow_dirs: list = []
            subagents: list = []
            routers: list = []

        return _R()

    import graph.plugins as gp

    monkeypatch.setattr(gp, "load_plugins", _fake_load_plugins)
    agent_init._build_plugins(LangGraphConfig())

    assert seen["config_getter"] is not None, "register() must see a live HOST.config on cold boot"
    assert seen["config_getter"]() is STATE.graph_config  # deferred read, not a snapshot
    assert seen["apply"] is not None
