"""A per-slot gateway alias opts out of an inherited native OAuth provider (#2550).

Selecting `model.provider: anthropic-oauth` (ADR 0097) applied it to every slot that
inherits — `aux_model`, `compaction.model`, `goal.eval_model`, each subagent, and the
`routing.fallback_models` chain — and a gateway alias in one of those RAISED rather than
routing, because the native builders reject any name containing "/".

That left a subscription-backed agent with exactly one lane and no degrade path:
LiteLLM's `fallbacks:` chain can't see a request that bypasses the gateway, and
protoAgent has no app-side failover by design (`graph/llm.py`: "the gateway owns
retries/fallbacks"). The rule is right when a gateway is always downstream; with a
native provider there is nothing downstream at all.
"""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.llm import create_llm

_GATEWAY = "https://gw.example.com/v1"


def _native(**over) -> LangGraphConfig:
    return LangGraphConfig(
        **{
            "model_provider": "anthropic-oauth",
            "model_name": "claude-opus-4-6",
            "api_base": _GATEWAY,
            "api_key": "gw-key",
            **over,
        }
    )


@pytest.fixture(autouse=True)
def _signed_in(monkeypatch):
    """A resolvable subscription credential, so nothing here fails for want of auth."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-X")


def test_a_gateway_alias_in_a_slot_routes_through_the_gateway():
    """The mixed native-main / gateway-aux case ADR 0097 left as a follow-up."""
    llm = create_llm(_native(), model_name="protolabs/coder")

    assert llm.model_name == "protolabs/coder"
    assert str(llm.openai_api_base).rstrip("/") == _GATEWAY.rstrip("/")


def test_a_native_model_id_in_a_slot_still_inherits_the_provider():
    """The "/" is the discriminator, exactly as it already is for gateway aliases vs
    native model ids — an unnamespaced id must not start leaking to the gateway."""
    llm = create_llm(_native(), model_name="claude-haiku-4-5")

    assert not hasattr(llm, "openai_api_base") or "protolabs" not in str(getattr(llm, "model_name", ""))
    assert "claude" in str(getattr(llm, "model", getattr(llm, "model_name", "")))


def test_the_main_model_must_still_be_a_real_provider_model():
    """Only an explicit per-slot override opts out. A gateway alias as THE model under a
    native provider stays a misconfiguration with a clear error — the slot escape hatch
    must not turn it into a silent gateway fallback."""
    with pytest.raises(RuntimeError, match="not a Claude model id"):
        create_llm(_native(model_name="protolabs/reasoning"))


def test_without_a_gateway_key_the_alias_cannot_override(monkeypatch):
    """Falling through would build a client against an empty gateway. The native builder
    raises its own clear error instead, and the ignored alias is logged rather than
    silently dropped."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _native()
    cfg.api_key = ""

    with pytest.raises(RuntimeError, match="not a Claude model id"):
        create_llm(cfg, model_name="protolabs/coder")


def test_the_operators_actual_ask_is_now_expressible():
    """Verbatim from the issue: "sonnet 5 for review with a fallback to our
    protolabs/coder model and openai gateway".

    `routing.fallback_models` already drives LangChain's ModelFallbackMiddleware — the
    failover machinery was there all along. It was unusable under a native provider only
    because building a gateway-alias fallback raised, which took the whole graph build
    down with it."""
    from langchain.agents.middleware import ModelFallbackMiddleware

    from graph.agent import _build_middleware

    cfg = _native(model_name="claude-sonnet-5", routing_fallback_models=["protolabs/coder"])

    chain = _build_middleware(cfg)

    assert any(isinstance(mw, ModelFallbackMiddleware) for mw in chain), (
        "the native-provider agent got no fallback middleware"
    )


# ── load-time coherence: detect early, reconcile the stale slot (bd-66cw) ──────
#
# A provider switch to a native OAuth provider (`model.provider: anthropic-oauth`) can
# leave a stale gateway alias (`protolabs/reasoning`) in a NON-lead slot — aux, a subagent
# tier, or a fallback — from another config layer. The LEAD pair is coherent, so the agent
# chats fine, but the FIRST `task`/subagent dispatch RAISED "is not a Claude model id",
# silently disabling every task/task_batch/workflow/board-dispatch delegation. These pin
# that (with NO gateway key to route the alias) the incoherence is surfaced AND the fixable
# slots are reconciled at config LOAD — not discovered on a live delegation.


def _oauth_dict(**over) -> dict:
    """A minimal from_dict doc: native provider, coherent lead, no gateway api_key."""
    model = {"provider": "anthropic-oauth", "name": "claude-opus-4-6", **over.pop("model", {})}
    return {"model": model, **over}


def test_coherence_warnings_flags_a_stale_alias_in_a_nonlead_slot(monkeypatch):
    """r1: a native-OAuth provider with a '/'-alias aux slot (no gateway) is reported by
    the load-time coherence check, naming the offending slot — read-only, no build."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LangGraphConfig(
        model_provider="anthropic-oauth", model_name="claude-opus-4-6", aux_model="protolabs/reasoning"
    )

    warnings = cfg.coherence_warnings()

    assert any("aux_model" in w and "protolabs/reasoning" in w for w in warnings), warnings


def test_from_dict_reports_and_reconciles_a_stale_aux_alias(monkeypatch, caplog):
    """r1 + r2: from_dict WARNs (reported at load, naming the slot) and clears the alias so
    the slot inherits the coherent lead model.name instead of the gateway-alias default."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with caplog.at_level("WARNING"):
        cfg = LangGraphConfig.from_dict(_oauth_dict(routing={"aux_model": "protolabs/reasoning"}))

    assert any("aux_model" in r.getMessage() for r in caplog.records), caplog.text
    assert cfg.aux_model == ""  # cleared → inherits the lead
    assert cfg.coherence_warnings() == []  # now coherent


def test_empty_override_slot_inherits_the_lead_claude_id_not_the_default(monkeypatch):
    """r2 + r5: a subagent tier alias is reconciled away, and an EMPTY override then
    resolves to the lead's Claude id — never the `protolabs/reasoning` dataclass default."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from graph.agent import _resolve_aux_model
    from graph.providers.anthropic_oauth import resolve_claude_model_name

    cfg = LangGraphConfig.from_dict(_oauth_dict(subagents={"researcher": {"model": "protolabs/reasoning"}}))

    assert cfg.researcher.model == ""  # the stale tier alias was reconciled away
    # An empty override (tier → aux → main) resolves to the coherent lead id, not the default.
    sub_model = _resolve_aux_model(cfg, cfg.researcher.model)  # both empty → None → main
    assert resolve_claude_model_name(cfg, sub_model) == "claude-opus-4-6"
    assert resolve_claude_model_name(cfg, None) == "claude-opus-4-6"


def test_dispatch_no_longer_raises_from_a_reconciled_slot(monkeypatch):
    """r3: on a coherent-lead instance, resolving a reconciled aux/subagent slot yields the
    lead Claude id — no "model.name='protolabs/reasoning' is not a Claude model id"."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from graph.agent import _resolve_aux_model
    from graph.providers.anthropic_oauth import resolve_claude_model_name

    cfg = LangGraphConfig.from_dict(_oauth_dict(routing={"aux_model": "protolabs/reasoning"}))

    # This is exactly what _run_subagent does: create_llm(config, model_name=<resolved>).
    assert resolve_claude_model_name(cfg, _resolve_aux_model(cfg, cfg.researcher.model)) == "claude-opus-4-6"


def test_a_stale_fallback_alias_is_dropped(monkeypatch):
    """A '/'-alias fallback would crash the graph build (each fallback is built eagerly),
    so an incoherent one is dropped while a coherent native fallback is kept."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LangGraphConfig.from_dict(
        _oauth_dict(model={"name": "claude-sonnet-5"}, routing={"fallback_models": ["protolabs/coder", "claude-haiku-4-5"]})
    )

    assert cfg.routing_fallback_models == ["claude-haiku-4-5"]
    assert cfg.coherence_warnings() == []


def test_a_gateway_provider_instance_is_unaffected(monkeypatch):
    """r4: on a gateway provider a '/'-alias slot is VALID — the check is provider-aware,
    not a blanket ban on '/'. Nothing is flagged and nothing is cleared."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LangGraphConfig.from_dict(
        {
            "model": {"provider": "openai", "name": "protolabs/reasoning"},
            "routing": {"aux_model": "protolabs/fast", "fallback_models": ["protolabs/coder"]},
        }
    )

    assert cfg.coherence_warnings() == []
    assert cfg.aux_model == "protolabs/fast"  # untouched
    assert cfg.routing_fallback_models == ["protolabs/coder"]  # untouched


def test_a_gateway_key_makes_the_alias_slot_coherent(monkeypatch):
    """With a gateway key the alias slot routes THROUGH the gateway (#2550), so it is
    coherent — neither flagged nor cleared even under a native provider."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LangGraphConfig.from_dict(
        _oauth_dict(model={"api_key": "gw-key"}, routing={"aux_model": "protolabs/reasoning"})
    )

    assert cfg.coherence_warnings() == []
    assert cfg.aux_model == "protolabs/reasoning"  # left intact — routes via the gateway
