"""ADR 0128 S1b: sparse config overlays for every registered worker."""
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from graph.config import LangGraphConfig, SubagentDef
from graph.subagents.config import REVIEW_FINDER_CONFIG, SUBAGENT_REGISTRY, SubagentConfig
from server import agent_init


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    import graph.subagents.config as sub

    monkeypatch.setattr(sub, "SUBAGENT_REGISTRY", dict(SUBAGENT_REGISTRY))
    monkeypatch.setattr(agent_init, "_subagent_static_defaults", {})
    monkeypatch.setattr(agent_init, "_disabled_config_subagents", set())
    monkeypatch.setattr(agent_init, "_subagent_overlay_warnings", set())
    monkeypatch.setattr(agent_init, "_plugin_subagent_names", set())
    return sub.SUBAGENT_REGISTRY


def test_default_profiles_and_noop(isolated_registry):
    before = deepcopy(isolated_registry)
    assert SubagentDef().profile == SubagentDef().final_profile == ""
    assert all(not spec.profile and not spec.final_profile for spec in before.values())
    agent_init._apply_config_subagents(LangGraphConfig())
    assert isolated_registry == before


def test_review_finder_init_reload_and_clear(isolated_registry):
    cfg = LangGraphConfig.from_dict({
        "model": {"profiles": {"lane": {}, "final": {}}},
        "subagents": {"review-finder": {"profile": "lane", "final_profile": "final"}},
    })
    for _ in range(2):
        agent_init._apply_config_subagents(cfg)
        assert isolated_registry["review-finder"] == replace(
            REVIEW_FINDER_CONFIG, profile="lane", final_profile="final"
        )
    cfg.subagent_overrides["review-finder"] = {"final_profile": "lane"}
    agent_init._apply_config_subagents(cfg)
    assert isolated_registry["review-finder"] == replace(REVIEW_FINDER_CONFIG, final_profile="lane")
    agent_init._apply_config_subagents(LangGraphConfig())
    assert isolated_registry["review-finder"] == REVIEW_FINDER_CONFIG


def test_plugin_snapshot_and_all_overlay_fields(isolated_registry):
    plugin = SubagentConfig(
        name="plugin-worker", description="plugin", system_prompt="Do work",
        tools=["read_file"], max_turns=9, model="original", allow_skill_emission=False,
    )
    agent_init._register_plugin_subagents([plugin])
    cfg = LangGraphConfig.from_dict({
        "model": {"profiles": {"lane": {}, "final": {}}},
        "subagents": {plugin.name: {
            "enabled": True, "tools": ["current_time"], "max_turns": 3,
            "model": "custom", "profile": "lane", "final_profile": "final",
        }},
    })
    expected = replace(plugin, tools=["current_time"], max_turns=3, model="custom", profile="lane", final_profile="final")
    for _ in range(2):
        agent_init._apply_config_subagents(cfg)
        assert isolated_registry[plugin.name] == expected
    # A registration-owned mutable list must not change the saved static default.
    plugin.tools.append("write_file")
    cfg.subagent_overrides[plugin.name] = {"enabled": False}
    agent_init._apply_config_subagents(cfg)
    assert plugin.name not in isolated_registry
    agent_init._apply_config_subagents(LangGraphConfig())
    assert isolated_registry[plugin.name] == replace(plugin, tools=["read_file"])


def test_plugin_rereg_with_changed_config_refreshes_static_default(isolated_registry):
    """A reload that re-registers a plugin subagent with a CHANGED SubagentConfig must be
    picked up — not masked by the boot-time snapshot the overlay derives from."""
    first = SubagentConfig(
        name="plugin-worker", description="plugin", system_prompt="Do work",
        tools=["read_file"], max_turns=9, model="original",
    )
    agent_init._register_plugin_subagents([first])
    cfg = LangGraphConfig.from_dict({"subagents": {"plugin-worker": {"profile": "lane"}}})
    agent_init._apply_config_subagents(cfg)
    assert isolated_registry["plugin-worker"] == replace(first, profile="lane")

    # Reload: the plugin re-registers a changed spec before the overlay re-applies. The
    # overlay must now derive from the NEW registration, not the original snapshot.
    changed = SubagentConfig(
        name="plugin-worker", description="plugin", system_prompt="Do work",
        tools=["current_time"], max_turns=5, model="updated",
    )
    agent_init._register_plugin_subagents([changed])
    agent_init._apply_config_subagents(cfg)
    assert isolated_registry["plugin-worker"] == replace(changed, profile="lane")
    # And still idempotent once the new spec is in place (no second re-registration).
    agent_init._apply_config_subagents(cfg)
    assert isolated_registry["plugin-worker"] == replace(changed, profile="lane")


def test_unknown_names_warn_once_and_keep_profiles(isolated_registry, caplog):
    cfg = LangGraphConfig.from_dict({"subagents": {
        "missing-worker": {"profile": "missing"},
        "review-finder": {"profile": "missing", "final_profile": "missing"},
    }})
    for _ in range(2):
        agent_init._apply_config_subagents(cfg)
    assert "missing-worker" not in isolated_registry
    assert isolated_registry["review-finder"].profile == "missing"
    assert isolated_registry["review-finder"].final_profile == "missing"
    warnings = [record.message for record in caplog.records if record.name == agent_init.log.name]
    assert len(warnings) == 2
    assert any("missing-worker" in message for message in warnings)
    assert any("Unknown model profile 'missing'" in message for message in warnings)


def test_researcher_profiles(isolated_registry):
    cfg = LangGraphConfig.from_dict({"subagents": {"researcher": {"profile": "lane", "final_profile": "final"}}})
    agent_init._apply_config_subagents(cfg)
    assert asdict(isolated_registry["researcher"])["profile"] == "lane"
    assert isolated_registry["researcher"].final_profile == "final"


def test_builtin_disable_restore_and_explicit_empty_tools(isolated_registry):
    cfg = LangGraphConfig.from_dict({"subagents": {"review-finder": {"enabled": False}}})
    agent_init._apply_config_subagents(cfg)
    assert "review-finder" not in isolated_registry
    cfg.subagent_overrides["review-finder"] = {"tools": []}
    agent_init._apply_config_subagents(cfg)
    assert isolated_registry["review-finder"].tools == []
    agent_init._apply_config_subagents(LangGraphConfig())
    assert isolated_registry["review-finder"] == REVIEW_FINDER_CONFIG


def test_removed_plugin_is_not_resurrected(isolated_registry):
    plugin = SubagentConfig(name="removed-plugin", description="plugin", system_prompt="Work")
    agent_init._register_plugin_subagents([plugin])
    agent_init._apply_config_subagents(LangGraphConfig())
    isolated_registry.pop(plugin.name)
    agent_init._apply_config_subagents(LangGraphConfig())
    assert plugin.name not in isolated_registry


def test_generalized_model_overlay_keeps_provider_coherence(monkeypatch, caplog):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LangGraphConfig.from_dict({
        "model": {"provider": "anthropic-oauth", "name": "claude-opus-4-6"},
        "subagents": {"review-finder": {"model": "protolabs/reasoning", "profile": "lane"}},
    })
    assert cfg.subagent_overrides["review-finder"] == {"model": "", "profile": "lane"}
    assert "subagents.review-finder.model" in caplog.text
