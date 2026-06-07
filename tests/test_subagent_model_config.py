"""Per-subagent model override via config (ADR 0001). _apply_config_subagents applies
subagents.<name>.model onto the runtime SUBAGENT_REGISTRY; _run_subagent already
resolves per-subagent → routing.aux_model → main model."""

from __future__ import annotations

import dataclasses

import yaml

from graph.config import LangGraphConfig
from graph.subagents.config import RESEARCHER_CONFIG, SUBAGENT_REGISTRY
from server.agent_init import _apply_config_subagents


def test_config_parses_subagent_model(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"subagents": {"researcher": {"model": "protolabs/reasoning"}}}))
    cfg = LangGraphConfig.from_yaml(str(p))
    assert cfg.researcher.model == "protolabs/reasoning"


def test_apply_sets_model_preserving_tools_and_prompt():
    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        cfg = LangGraphConfig()
        cfg.researcher = dataclasses.replace(cfg.researcher, model="protolabs/reasoning")
        _apply_config_subagents(cfg)
        entry = SUBAGENT_REGISTRY["researcher"]
        assert entry.model == "protolabs/reasoning"
        assert entry.tools == RESEARCHER_CONFIG.tools          # tools untouched (model-only)
        assert entry.system_prompt == RESEARCHER_CONFIG.system_prompt
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original


def test_blank_model_is_base_and_idempotent():
    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        _apply_config_subagents(LangGraphConfig())            # default, model=""
        assert SUBAGENT_REGISTRY["researcher"].model == RESEARCHER_CONFIG.model
        cfg = LangGraphConfig()
        cfg.researcher = dataclasses.replace(cfg.researcher, model="x")
        _apply_config_subagents(cfg)
        assert SUBAGENT_REGISTRY["researcher"].model == "x"
        _apply_config_subagents(LangGraphConfig())            # cleared → reverts to base
        assert SUBAGENT_REGISTRY["researcher"].model == RESEARCHER_CONFIG.model
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original


# ── full override wiring (tools / max_turns / enabled), no drift ──────────────

def test_default_config_preserves_registry_tools_no_drift():
    """An un-overridden config must equal the registry default — incl. memory_ingest,
    which the old hardcoded config default was missing (the drift bug)."""
    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        _apply_config_subagents(LangGraphConfig())
        assert SUBAGENT_REGISTRY["researcher"].tools == RESEARCHER_CONFIG.tools
        assert "memory_ingest" in SUBAGENT_REGISTRY["researcher"].tools
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original


def test_tools_and_max_turns_override_applies(tmp_path):
    import yaml as y
    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        p = tmp_path / "c.yaml"
        p.write_text(y.safe_dump({"subagents": {"researcher": {"tools": ["current_time"], "max_turns": 7}}}))
        cfg = LangGraphConfig.from_yaml(str(p))
        _apply_config_subagents(cfg)
        entry = SUBAGENT_REGISTRY["researcher"]
        assert entry.tools == ["current_time"] and entry.max_turns == 7
        assert entry.system_prompt == RESEARCHER_CONFIG.system_prompt  # base preserved
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original


def test_disabled_removes_subagent():
    import dataclasses
    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        cfg = LangGraphConfig()
        cfg.researcher = dataclasses.replace(cfg.researcher, enabled=False)
        _apply_config_subagents(cfg)
        assert "researcher" not in SUBAGENT_REGISTRY
        # re-enable restores from base
        _apply_config_subagents(LangGraphConfig())
        assert "researcher" in SUBAGENT_REGISTRY
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original
