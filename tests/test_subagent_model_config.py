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
