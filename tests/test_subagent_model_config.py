"""Per-subagent model override via config (ADR 0001). _apply_config_subagents applies
subagents.<name>.model onto the runtime SUBAGENT_REGISTRY; _run_subagent already
resolves per-subagent → routing.aux_model → main model."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import yaml
from langchain.agents.middleware import ModelFallbackMiddleware

import graph.agent as agent_mod
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
        assert entry.tools == RESEARCHER_CONFIG.tools  # tools untouched (model-only)
        assert entry.system_prompt == RESEARCHER_CONFIG.system_prompt
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original


def test_blank_model_is_base_and_idempotent():
    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        _apply_config_subagents(LangGraphConfig())  # default, model=""
        assert SUBAGENT_REGISTRY["researcher"].model == RESEARCHER_CONFIG.model
        cfg = LangGraphConfig()
        cfg.researcher = dataclasses.replace(cfg.researcher, model="x")
        _apply_config_subagents(cfg)
        assert SUBAGENT_REGISTRY["researcher"].model == "x"
        _apply_config_subagents(LangGraphConfig())  # cleared → reverts to base
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


def test_allow_skill_emission_defaults_true_and_is_settable():
    """The opt-out field (#1347) defaults True so stock subagents stay distillable,
    and is settable so a plugin/fork can mark a subagent's runs non-emittable."""
    from graph.subagents.config import SubagentConfig

    assert RESEARCHER_CONFIG.allow_skill_emission is True
    opted_out = SubagentConfig(name="verdict", description="d", system_prompt="p", allow_skill_emission=False)
    assert opted_out.allow_skill_emission is False


def test_config_overlay_preserves_allow_skill_emission(monkeypatch):
    """A YAML model/tools override must not RESET allow_skill_emission to the dataclass
    default — _apply_config_subagents rebuilds via replace(base, ...). Seed the base with
    the flag OFF (the non-default) so this fails if a refactor reconstructs the config
    without carrying it through, not just when it happens to match the default."""
    import graph.subagents.config as sub_config

    original = SUBAGENT_REGISTRY.get("researcher")
    # _apply_config_subagents reads the module-level RESEARCHER_CONFIG as its base.
    monkeypatch.setattr(sub_config, "RESEARCHER_CONFIG", dataclasses.replace(RESEARCHER_CONFIG, allow_skill_emission=False))
    try:
        cfg = LangGraphConfig()
        cfg.researcher = dataclasses.replace(cfg.researcher, model="protolabs/reasoning")
        _apply_config_subagents(cfg)
        entry = SUBAGENT_REGISTRY["researcher"]
        assert entry.model == "protolabs/reasoning"  # overlay applied
        assert entry.allow_skill_emission is False  # base flag preserved, NOT reset to default True
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original
        else:
            SUBAGENT_REGISTRY.pop("researcher", None)  # don't leave a temp entry behind


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


# ── routing.fallback_models failover on the subagent stack (#2995) ────────────
#
# routing.fallback_models used to wire the failover chain onto the LEAD agent's
# middleware only (_build_middleware); a subagent's model error therefore failed
# the whole delegation instead of retrying the next model. The subagent stack
# now mirrors the lead: it appends ObservableModelFallbackMiddleware (built from
# the same routing.fallback_models) when the list is non-empty, and stays
# unchanged when it's empty.


def _names(mws) -> list[str]:
    return [type(m).__name__ for m in mws]


def _capture_subagent_middleware(monkeypatch, **cfg_kwargs) -> list:
    """Drive _run_subagent far enough to capture the middleware list it hands to
    create_agent, without a model or a real graph (same harness as
    test_subagent_native_oauth.py)."""
    seen: dict = {}

    def fake_create_agent(**kwargs):
        seen["middleware"] = kwargs.get("middleware") or []

        class _Agent:
            async def ainvoke(self, *_a, **_kw):
                return {"messages": [SimpleNamespace(content="ok", type="ai")]}

        return _Agent()

    monkeypatch.setattr(agent_mod, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_kw: object())

    cfg = LangGraphConfig(**cfg_kwargs)
    tool = SimpleNamespace(name="current_time")
    try:
        asyncio.run(
            agent_mod._run_subagent(
                config=cfg,
                tool_map={"current_time": tool},
                available_subagents="researcher",
                prompt="go",
                subagent_type="researcher",
                description="delegation under test",
            )
        )
    except Exception:  # noqa: BLE001 — the run may fail past create_agent; we only need the list
        pass
    return seen.get("middleware", [])


def test_subagent_carries_fallback_middleware_when_configured(monkeypatch):
    """GIVEN routing.fallback_models WHEN a delegation runs THEN the subagent's
    middleware carries ModelFallbackMiddleware — the same failover chain the lead
    agent gets — so a primary-model error retries the next model instead of
    failing the delegation."""
    mws = _capture_subagent_middleware(
        monkeypatch, model_provider="openai", routing_fallback_models=["gpt-4o-mini", "gpt-4o"]
    )
    # ObservableModelFallbackMiddleware subclasses the stock middleware, so an
    # isinstance check pins the failover contract regardless of the wrapper name.
    assert any(isinstance(m, ModelFallbackMiddleware) for m in mws), _names(mws)


def test_subagent_has_no_fallback_when_unconfigured(monkeypatch):
    """AND when routing.fallback_models is empty, subagent behavior is unchanged —
    no fallback middleware is mounted."""
    mws = _capture_subagent_middleware(monkeypatch, model_provider="openai", routing_fallback_models=[])
    assert not any(isinstance(m, ModelFallbackMiddleware) for m in mws), _names(mws)


def test_subagent_fallback_is_built_from_configured_models(monkeypatch):
    """The failover chain is built from routing.fallback_models — one fallback LLM
    per configured model name, resolved via create_llm(config, model_name=m)."""
    created: list = []

    def fake_create_llm(_cfg, *, model_name=None, **_kw):
        created.append(model_name)
        return object()

    monkeypatch.setattr(agent_mod, "create_agent", lambda **_kw: _StubAgent())
    monkeypatch.setattr(agent_mod, "create_llm", fake_create_llm)

    seen: dict = {}
    orig_fallback = agent_mod.ObservableModelFallbackMiddleware

    def record_fallback(*models):
        seen["count"] = len(models)
        return orig_fallback(*models)

    monkeypatch.setattr(agent_mod, "ObservableModelFallbackMiddleware", record_fallback)

    cfg = LangGraphConfig(model_provider="openai", routing_fallback_models=["fast", "slow"])
    tool = SimpleNamespace(name="current_time")
    try:
        asyncio.run(
            agent_mod._run_subagent(
                config=cfg,
                tool_map={"current_time": tool},
                available_subagents="researcher",
                prompt="go",
                subagent_type="researcher",
                description="delegation under test",
            )
        )
    except Exception:  # noqa: BLE001 — only the fallback construction matters here
        pass
    # One fallback LLM per configured model name, and both names were resolved.
    assert seen.get("count") == 2, seen
    assert "fast" in created and "slow" in created, created


class _StubAgent:
    async def ainvoke(self, *_a, **_kw):
        return {"messages": [SimpleNamespace(content="ok", type="ai")]}
