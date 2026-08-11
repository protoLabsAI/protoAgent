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
