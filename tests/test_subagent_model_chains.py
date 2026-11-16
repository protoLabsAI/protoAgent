"""A subagent tier's pinned model may be a FALLBACK CHAIN (ADR 0133 M5).

``subagents.<name>.model`` accepted a single id; it now also accepts an ordered list — the
primary, then each fallback used only on a retryable provider error. The pin still wins the
``graph.subagent_model`` precedence (pin > turn override > routing.aux_model > main); a list
pin resolves to its primary there and is built into a fallback runnable by the dispatch path
(``build_chain_llm``), so a 429 on the primary fails over to the next model INSTEAD of
failing the whole delegation. A scalar pin stays a chain of one (today's behavior), and a
tier with no pin inherits the aux→main chain with the main chain's fallbacks unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableLambda

import graph.agent as agent_mod
from graph import model_roles
from graph.config import LangGraphConfig, coherent_model_chain
from graph.model_roles import _RoleLLM
from graph.subagent_model import resolve_subagent_model, subagent_pin_chain
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig


class _ProviderError(Exception):
    def __init__(self, status: int):
        super().__init__("sensitive provider payload")
        self.status_code = status


@pytest.fixture(autouse=True)
def _reset_diagnostics(monkeypatch):
    # The role-LLM fallback records a streak/last-fallback per role; isolate each test.
    monkeypatch.setattr(model_roles, "_last_fallbacks", {})
    monkeypatch.setattr(model_roles, "_streaks", set())
    # OPENAI_API_KEY would make the native-provider coherence check see a gateway key;
    # PROTOAGENT_MODEL would overwrite model_name in __post_init__.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PROTOAGENT_MODEL", raising=False)


# ── resolver: a list pin's PRIMARY wins precedence (chain-aware pin reading) ─────


def test_resolve_subagent_model_reads_a_list_pins_primary():
    cfg = LangGraphConfig()
    cfg.aux_model = "protolabs/fast"
    # A list pin wins over the turn override, resolving to its PRIMARY entry — before the fix
    # `_clean` dropped the whole list to "" and the pin silently lost to the override/aux.
    assert resolve_subagent_model(cfg, "ignored", "override", pinned=["pa", "pb"]) == "pa"
    # A blank/all-blank list pin is as empty as "" → the override, then the aux primary.
    assert resolve_subagent_model(cfg, "ignored", "override", pinned=["", "  "]) == "override"
    assert resolve_subagent_model(cfg, "ignored", "", pinned=[]) == "protolabs/fast"


def test_registry_list_pin_resolves_to_its_primary(monkeypatch):
    # The pinned= arg is omitted (None) → the pin is read from the registry. This is the
    # background-fire path (background.manager carries the resolved primary in fire metadata);
    # a list pin now yields its primary instead of `_clean` dropping the whole list to "".
    from graph.subagent_model import pinned_subagent_model

    sub = SubagentConfig(name="stub", description="d", system_prompt="p", model=["pa", "pb"])
    monkeypatch.setitem(SUBAGENT_REGISTRY, "stub", sub)
    assert pinned_subagent_model("stub") == "pa"
    assert resolve_subagent_model(LangGraphConfig(), "stub", "override") == "pa"  # pin > override


def test_subagent_pin_chain_normalizes_scalar_list_and_blanks():
    cfg = LangGraphConfig()
    assert subagent_pin_chain(cfg, ["a", "b"]) == ["a", "b"]
    assert subagent_pin_chain(cfg, "a") == ["a"]  # a scalar is a chain of one
    assert subagent_pin_chain(cfg, "") == []  # no pin → inherit (empty chain)
    assert subagent_pin_chain(cfg, ["", "  "]) == []  # all blank → no pin
    assert subagent_pin_chain(cfg, [" a ", "", "b"]) == ["a", "b"]  # stripped, blanks dropped


def test_subagent_pin_chain_drops_incoherent_entries_under_native_provider():
    # Native-OAuth provider, no gateway key: a bare '/'-alias can't route, so it's dropped
    # per-entry (the same coherence role_chain applies), leaving the coherent tail.
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-main")  # api_key="" by default
    assert subagent_pin_chain(cfg, ["claude-reason", "protolabs/alias"]) == ["claude-reason"]
    # Mirror of graph.config.coherent_model_chain, the single per-entry reader.
    assert subagent_pin_chain(cfg, ["claude-reason", "protolabs/alias"]) == coherent_model_chain(
        cfg, ["claude-reason", "protolabs/alias"], where="subagent.model"
    )


# ── dispatch: a tier pinned to [a(429), b] answers from b ────────────────────────


def _capture_sub_llm(monkeypatch, *, pin, models):
    """Drive ``_run_subagent`` far enough to capture the ``model`` (the resolved sub-LLM) it
    hands to ``create_agent`` — the same harness shape as test_subagent_model_config.py. The
    fake ``create_llm`` resolves each chain entry to its prepared fake model."""
    seen: dict = {}

    def fake_create_agent(**kwargs):
        seen["model"] = kwargs.get("model")

        class _Agent:
            async def ainvoke(self, *_a, **_kw):
                return {"messages": [SimpleNamespace(content="ok", type="ai")]}

        return _Agent()

    def fake_create_llm(_cfg, *, model_name=None, **_kw):
        return models.get(model_name, object())

    monkeypatch.setattr(agent_mod, "create_agent", fake_create_agent)
    # build_chain_llm lives in model_roles and calls ITS module-level create_llm, while the
    # single-model branch calls agent's — patch both so either path resolves the fakes.
    monkeypatch.setattr(agent_mod, "create_llm", fake_create_llm)
    monkeypatch.setattr(model_roles, "create_llm", fake_create_llm)

    cfg = LangGraphConfig(model_provider="openai")
    sub = SubagentConfig(name="stub", description="d", system_prompt="p", tools=["current_time"], model=pin)
    monkeypatch.setitem(SUBAGENT_REGISTRY, "stub", sub)
    tool = SimpleNamespace(name="current_time")
    try:
        asyncio.run(
            agent_mod._run_subagent(
                config=cfg,
                tool_map={"current_time": tool},
                available_subagents="stub",
                prompt="go",
                subagent_type="stub",
                description="delegation under test",
            )
        )
    except Exception:  # noqa: BLE001 — the fake agent may fail past create_agent; only the model matters
        pass
    return seen.get("model")


def test_pinned_chain_tier_fails_over_to_the_second_model(monkeypatch):
    calls: list[str] = []

    def answer(name, result):
        def run(_value):
            calls.append(name)
            if isinstance(result, Exception):
                raise result
            return result

        return RunnableLambda(run)

    models = {"a": answer("a", _ProviderError(429)), "b": answer("b", "from-b")}
    sub_llm = _capture_sub_llm(monkeypatch, pin=["a", "b"], models=models)

    # The pinned chain built a fallback runnable over its OWN two models…
    assert isinstance(sub_llm, _RoleLLM)
    assert sub_llm.names == ["a", "b"]
    # …and a 429 on the primary fails over to the second, which answers.
    assert sub_llm.invoke("prompt") == "from-b"
    assert calls == ["a", "b"]
    record = model_roles.last_fallback("subagent:stub")
    assert record and record["from"] == ["a"] and record["to"] == "b"


def test_scalar_pin_tier_stays_a_single_model_no_chain(monkeypatch):
    # A scalar pin is a chain of one: the single-model branch, NOT a fallback runnable.
    models = {"only": RunnableLambda(lambda _v: "ok")}
    sub_llm = _capture_sub_llm(monkeypatch, pin="only", models=models)
    assert sub_llm is models["only"]


def test_unpinned_tier_stays_a_single_model(monkeypatch):
    # No pin → inherit (empty pin chain) → the single-model branch on the resolved default
    # (here None → the main model). The main chain's fallbacks still ride the middleware.
    sentinel = object()
    sub_llm = _capture_sub_llm(monkeypatch, pin="", models={None: sentinel})
    assert sub_llm is sentinel


# ── config: subagents.researcher.model accepts a list and applies without crashing ──


def test_researcher_model_parses_as_a_list(tmp_path):
    import yaml

    from server.agent_init import _apply_config_subagents

    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"subagents": {"researcher": {"model": ["protolabs/a", "protolabs/b"]}}}))
    cfg = LangGraphConfig.from_yaml(str(p))
    assert cfg.researcher.model == ["protolabs/a", "protolabs/b"]

    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        # The researcher special-case overlay once did `(model or "").strip()` — a list would
        # crash there; the fix keeps the list verbatim on the registry entry.
        _apply_config_subagents(cfg)
        assert SUBAGENT_REGISTRY["researcher"].model == ["protolabs/a", "protolabs/b"]
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original


def test_researcher_blank_list_model_falls_back_to_base():
    import dataclasses

    from graph.subagents.config import RESEARCHER_CONFIG
    from server.agent_init import _apply_config_subagents

    original = SUBAGENT_REGISTRY.get("researcher")
    try:
        cfg = LangGraphConfig()
        cfg.researcher = dataclasses.replace(cfg.researcher, model=["", "  "])  # all-blank chain
        _apply_config_subagents(cfg)
        # An empty chain is as blank as "" → the base pin, exactly like a blank scalar.
        assert SUBAGENT_REGISTRY["researcher"].model == RESEARCHER_CONFIG.model
    finally:
        if original is not None:
            SUBAGENT_REGISTRY["researcher"] = original
