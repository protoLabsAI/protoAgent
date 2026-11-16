"""A subagent tier's pinned model may be a FALLBACK CHAIN (ADR 0133 M5).

``subagents.<name>.model`` now also accepts an ordered list — the primary, then each fallback
used only on a retryable provider error. The pin still wins ``graph.subagent_model`` precedence;
a list pin resolves to its PRIMARY (the real BaseChatModel ``create_agent`` binds tools onto) and
the dispatch wires the chain's tail onto the subagent's ``ObservableModelFallbackMiddleware`` so a
429 on the primary fails over. A scalar pin is a chain of one; an unpinned tier inherits the
aux→main chain with the main chain's fallbacks unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import graph.agent as agent_mod
from graph import model_roles
from graph.agent import ObservableModelFallbackMiddleware
from graph.config import LangGraphConfig
from graph.subagent_model import pinned_subagent_model, resolve_subagent_model, subagent_pin_chain
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig


class _ProviderError(Exception):
    def __init__(self, status: int):
        super().__init__("sensitive provider payload")
        self.status_code = status


class _FakeModel:
    """Breaker/account helpers key on ``model_name``; a real chat client is never built here."""

    def __init__(self, name: str):
        self.model_name = name


class _Req:
    def __init__(self, model):
        self.model = model

    def override(self, **kw):
        return _Req(kw.get("model", self.model))


@pytest.fixture(autouse=True)
def _reset_diagnostics(monkeypatch):
    monkeypatch.setattr(model_roles, "_last_fallbacks", {})
    monkeypatch.setattr(model_roles, "_streaks", set())
    agent_mod.reset_primary_breakers()  # a prior test's breaker trip must not route this primary
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # else read as a gateway key
    monkeypatch.delenv("PROTOAGENT_MODEL", raising=False)  # else overwrites model_name in __post_init__


def test_list_pin_resolves_to_its_primary(monkeypatch):
    cfg = LangGraphConfig()
    cfg.aux_model = "protolabs/fast"
    # A list pin wins over the turn override, resolving to its PRIMARY — before the fix `_clean`
    # dropped the whole list to "" and the pin silently lost to the override/aux.
    assert resolve_subagent_model(cfg, "ignored", "override", pinned=["pa", "pb"]) == "pa"
    assert resolve_subagent_model(cfg, "ignored", "override", pinned=["", "  "]) == "override"  # all-blank == no pin
    assert resolve_subagent_model(cfg, "ignored", "", pinned=[]) == "protolabs/fast"
    # pinned=None → read from the registry (the background-fire path); a list pin yields its primary.
    sub = SubagentConfig(name="stub", description="d", system_prompt="p", model=["pa", "pb"])
    monkeypatch.setitem(SUBAGENT_REGISTRY, "stub", sub)
    assert pinned_subagent_model("stub") == "pa"
    assert resolve_subagent_model(LangGraphConfig(), "stub", "override") == "pa"  # pin > override


def test_subagent_pin_chain_normalizes_and_drops_incoherent_entries():
    cfg = LangGraphConfig()
    assert subagent_pin_chain(cfg, ["a", "b"]) == ["a", "b"]
    assert subagent_pin_chain(cfg, "a") == ["a"]  # a scalar is a chain of one
    assert subagent_pin_chain(cfg, "") == []  # no pin → inherit (empty chain)
    assert subagent_pin_chain(cfg, [" a ", "", "b"]) == ["a", "b"]  # stripped; blanks dropped
    # Native-OAuth provider, no gateway key: a bare '/'-alias can't route, so it drops per-entry
    # (the coherence role_chain applies); an all-incoherent chain empties to [] → the tier inherits.
    native = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-main")
    assert subagent_pin_chain(native, ["claude-reason", "protolabs/alias"]) == ["claude-reason"]
    assert subagent_pin_chain(native, ["protolabs/a", "protolabs/b"]) == []
    # Under a gateway provider the same aliases route fine — kept verbatim.
    assert subagent_pin_chain(LangGraphConfig(model_provider="openai"), ["protolabs/a", "protolabs/b"]) == [
        "protolabs/a",
        "protolabs/b",
    ]


# ── dispatch: a tier pinned to [a(429), b] answers from b ────────────────────────


def _capture_dispatch(monkeypatch, *, pin, models, routing_fallbacks=(), config=None, turn_model=""):
    """Capture the ``model`` and fallback middleware ``_run_subagent`` hands to ``create_agent``
    (the harness shape of test_subagent_model_config.py); fake ``create_llm`` maps id→fake model."""
    seen: dict = {}

    def fake_create_agent(**kwargs):
        seen["model"] = kwargs.get("model")
        seen["middleware"] = kwargs.get("middleware") or []

        class _Agent:
            async def ainvoke(self, *_a, **_kw):
                return {"messages": [SimpleNamespace(content="ok", type="ai")]}

        return _Agent()

    monkeypatch.setattr(agent_mod, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_mod, "create_llm", lambda _c, *, model_name=None, **_k: models.get(model_name, object()))

    cfg = config or LangGraphConfig(model_provider="openai")
    cfg.routing_fallback_models = list(routing_fallbacks)
    sub = SubagentConfig(name="stub", description="d", system_prompt="p", tools=["current_time"], model=pin)
    monkeypatch.setitem(SUBAGENT_REGISTRY, "stub", sub)
    try:
        asyncio.run(
            agent_mod._run_subagent(
                config=cfg,
                tool_map={"current_time": SimpleNamespace(name="current_time")},
                available_subagents="stub",
                prompt="go",
                subagent_type="stub",
                description="delegation under test",
                turn_model=turn_model,
            )
        )
    except Exception:  # noqa: BLE001 — the fake agent fails past create_agent; only the capture matters
        pass
    mw = next((m for m in seen.get("middleware", []) if isinstance(m, ObservableModelFallbackMiddleware)), None)
    return seen.get("model"), mw


def test_pinned_chain_tier_fails_over_to_the_second_model(monkeypatch):
    # routing_fallbacks is set but must be IGNORED here: a tier that pins its OWN chain fails
    # over across the chain's tail, not the global fallbacks.
    models = {"a": _FakeModel("a"), "b": _FakeModel("b"), "glob": _FakeModel("glob")}
    model, fb = _capture_dispatch(monkeypatch, pin=["a", "b"], models=models, routing_fallbacks=["glob"])
    # The PRIMARY — a real (fake) BaseChatModel create_agent can bind_tools onto, NOT a
    # Runnable-only _RoleLLM that would fail the delegation before any failover. Its tail rides
    # the SAME observable middleware, keyed on its OWN names (not the globals).
    assert model is models["a"]
    assert fb is not None and fb.models == [models["b"]] and fb._fallback_names == ["b"]
    # A 429 on the primary fails over to "b", which answers — driven through the real middleware.
    order: list[str] = []

    def handler(req):
        order.append(req.model.model_name)
        if req.model.model_name == "a":
            raise _ProviderError(429)
        return AIMessage(content="from-b")

    out = fb.wrap_model_call(_Req(models["a"]), handler)
    assert out.content == "from-b"
    assert order == ["a", "b"]


def test_scalar_and_unpinned_tiers_use_the_global_fallbacks(monkeypatch):
    # A scalar pin is a chain of one: its primary + the GLOBAL routing.fallback_models, as before.
    models = {"only": _FakeModel("only"), "glob": _FakeModel("glob")}
    model, fb = _capture_dispatch(monkeypatch, pin="only", models=models, routing_fallbacks=["glob"])
    assert model is models["only"]
    assert fb is not None and fb.models == [models["glob"]] and fb._fallback_names == ["glob"]
    # No pin → inherit (here None → the main model); the main chain's fallbacks still ride the mw.
    sentinel = _FakeModel("main")
    _model, fb = _capture_dispatch(
        monkeypatch, pin="", models={None: sentinel, "glob": _FakeModel("glob")}, routing_fallbacks=["glob"]
    )
    assert _model is sentinel and fb._fallback_names == ["glob"]


@pytest.mark.parametrize(
    "pin,turn_model,aux,expected",
    [
        (["protolabs/a", "protolabs/b"], "claude-turn", "claude-aux", "claude-turn"),
        (["protolabs/a", "protolabs/b"], "", "claude-aux", "claude-aux"),
        (["protolabs/a", "protolabs/b"], "", "", None),
        (["protolabs/a", "claude-valid"], "claude-turn", "claude-aux", "claude-valid"),
    ],
)
def test_native_dispatch_inherits_when_pin_has_no_coherent_models(monkeypatch, pin, turn_model, aux, expected):
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-main")
    cfg.aux_model = aux
    models = {expected: _FakeModel(expected or "claude-main"), "claude-backup": _FakeModel("claude-backup")}
    model, fb = _capture_dispatch(
        monkeypatch, pin=pin, models=models, routing_fallbacks=["claude-backup"],
        config=cfg, turn_model=turn_model,
    )
    assert model is models[expected]
    assert fb is not None and fb.models == [models["claude-backup"]]
    # Registry-based resolution and usage attribution must agree with dispatch.
    assert resolve_subagent_model(cfg, "stub", turn_model) == expected


def test_researcher_model_parses_as_a_list_and_blank_falls_back(tmp_path):
    # subagents.researcher.model accepts a list (ADR 0133) and applies without the .strip() crash.
    import dataclasses

    import yaml

    from graph.subagents.config import RESEARCHER_CONFIG
    from server.agent_init import _apply_config_subagents

    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"subagents": {"researcher": {"model": ["protolabs/a", "protolabs/b"]}}}))
    cfg = LangGraphConfig.from_yaml(str(p))
    assert cfg.researcher.model == ["protolabs/a", "protolabs/b"]

    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        # The overlay once did `(model or "").strip()` (crashes on a list); now a set list is kept
        # verbatim, an all-blank chain is "" → the base pin.
        _apply_config_subagents(cfg)
        assert SUBAGENT_REGISTRY["researcher"].model == ["protolabs/a", "protolabs/b"]
        cfg.researcher = dataclasses.replace(cfg.researcher, model=["", "  "])
        _apply_config_subagents(cfg)
        assert SUBAGENT_REGISTRY["researcher"].model == RESEARCHER_CONFIG.model
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original


def test_researcher_chain_edit_keeps_enabled_and_profile(tmp_path):
    """Editing only the researcher chain in the ADR 0133 M7c Model-roles editor must not
    re-enable a disabled researcher or clear its profile (bd-yoj7g regression).

    ``researcher``'s pin is a typed config field, deliberately excluded from the
    ``subagent_overrides`` map the editor round-trips, so the editor saves a model-ONLY
    ``researcher`` member. The save write path (``apply_updates_to_yaml``) does a
    fixed-depth merge: it writes ``subagents.researcher.model`` and leaves the stored
    ``enabled`` / ``profile`` siblings untouched, and the loader inherits any key the
    submitted block omits from the stored spec — so ``enabled: false`` and the profile
    survive the chain edit instead of resetting to ``True`` / ``""``.
    """
    import yaml

    from graph.config_io import apply_updates_to_yaml, load_yaml_doc, save_yaml_doc

    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {"subagents": {"researcher": {"enabled": False, "profile": "deepthink", "model": "protolabs/old"}}}
        )
    )

    # The editor submits the whole subagents overrides dict; researcher carries ONLY its chain.
    doc = load_yaml_doc(p)
    apply_updates_to_yaml(doc, {"subagents": {"researcher": {"model": ["protolabs/a", "protolabs/b"]}}})
    save_yaml_doc(doc, p)

    cfg = LangGraphConfig.from_yaml(str(p))
    assert cfg.researcher.model == ["protolabs/a", "protolabs/b"]  # the chain IS updated
    assert cfg.researcher.enabled is False  # a disabled researcher stays off
    assert cfg.researcher.profile == "deepthink"  # the profile is not cleared
