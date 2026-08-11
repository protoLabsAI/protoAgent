"""Any slot can name its own provider: `<provider>:<model>` (#2550 follow-up).

#2550 let a slot opt out of an inherited native provider *toward the gateway*, using the
"/" in a gateway alias as the discriminator. That covers one direction. An operator who
holds all three — a gateway key, a Claude subscription and a ChatGPT subscription —
wants to mix freely: Claude for review, Codex for code, the gateway for cheap bulk work,
whatever the main brain is running on.

A bare model id can't express that once two native providers are configured
(`claude-sonnet-5` is unambiguous, `gpt-5.6-sol` is not — Codex and the gateway both
plausibly serve it). So slots accept an explicit provider prefix, extending the
`acp:<agent>` convention that already existed to the other three lanes.
"""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.llm import create_llm, split_slot_target

_GATEWAY = "https://gw.example.com/v1"


def _cfg(**over) -> LangGraphConfig:
    return LangGraphConfig(
        **{
            "model_provider": "",
            "model_name": "protolabs/reasoning",
            "api_base": _GATEWAY,
            "api_key": "gw-key",
            **over,
        }
    )


@pytest.fixture(autouse=True)
def _both_subscriptions(monkeypatch):
    """An operator signed in to everything — the case the qualified form exists for.

    Patches the name where it is USED (`openai_codex` imported it at module load), not
    where it is defined: patching `graph.providers.oauth.resolve_codex_oauth` passes when
    this file runs alone and fails in the full suite, purely on whether `openai_codex`
    had already been imported. Same target the sibling tests use."""
    import graph.providers.openai_codex as ocx
    from graph.providers.oauth import CodexOAuthCreds

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-X")
    monkeypatch.setattr(
        ocx,
        "resolve_codex_oauth",
        lambda *a, **k: CodexOAuthCreds(
            access_token="t", account_id="a", base_url="https://chatgpt.example/backend-api/codex", source="s"
        ),
    )


# ── the grammar ───────────────────────────────────────────────────────────────
def test_split_recognises_the_three_lanes():
    assert split_slot_target("gateway:protolabs/coder") == ("gateway", "protolabs/coder")
    assert split_slot_target("anthropic-oauth:claude-sonnet-5") == ("anthropic-oauth", "claude-sonnet-5")
    assert split_slot_target("openai-codex:gpt-5.6-sol") == ("openai-codex", "gpt-5.6-sol")
    assert split_slot_target(" OpenAI-Codex : gpt-5 ") == ("openai-codex", "gpt-5")


def test_split_leaves_every_existing_slot_value_alone():
    """Unqualified names must keep meaning exactly what they meant — this grammar is
    additive or it silently re-routes every configured fleet."""
    for unqualified in ("protolabs/coder", "claude-opus-4-6", "gpt-5.6-sol", "", None):
        provider, model = split_slot_target(unqualified)
        assert provider == "", unqualified
        assert model == (unqualified or "").strip()

    # `acp:` is handled upstream in create_llm and must NOT be claimed here.
    assert split_slot_target("acp:claude") == ("", "acp:claude")
    # An unknown prefix is a model name, not a provider — no silent misroute.
    assert split_slot_target("bedrock:anthropic.claude") == ("", "bedrock:anthropic.claude")


# ── dispatch ──────────────────────────────────────────────────────────────────
def test_a_gateway_slot_routes_to_the_gateway_from_a_native_main():
    llm = create_llm(
        _cfg(model_provider="anthropic-oauth", model_name="claude-opus-4-6"), model_name="gateway:protolabs/coder"
    )

    assert llm.model_name == "protolabs/coder"
    assert str(llm.openai_api_base).rstrip("/") == _GATEWAY.rstrip("/")


def test_a_claude_slot_routes_to_claude_from_a_gateway_main():
    """The direction #2550 could not express: main on the gateway, one slot on the
    Claude subscription."""
    llm = create_llm(_cfg(), model_name="anthropic-oauth:claude-sonnet-5")

    assert "claude-sonnet-5" in str(getattr(llm, "model", getattr(llm, "model_name", "")))
    assert not str(getattr(llm, "openai_api_base", "")).startswith(_GATEWAY)


def test_a_codex_slot_routes_to_codex_from_a_claude_main():
    """Two native providers at once — the case a bare model id cannot express."""
    llm = create_llm(
        _cfg(model_provider="anthropic-oauth", model_name="claude-opus-4-6"), model_name="openai-codex:gpt-5.6-sol"
    )

    assert "gpt-5.6-sol" in str(getattr(llm, "model", getattr(llm, "model_name", "")))
    assert "chatgpt.example" in str(getattr(llm, "openai_api_base", ""))


def test_the_qualified_form_beats_the_slash_shorthand():
    """`gateway:` is explicit; the "/" heuristic is a shorthand. When both could apply
    the explicit one must win, or the grammar has two answers for one input."""
    llm = create_llm(
        _cfg(model_provider="anthropic-oauth", model_name="claude-opus-4-6"), model_name="gateway:protolabs/coder"
    )

    assert llm.model_name == "protolabs/coder"


def test_a_gateway_slot_without_a_key_says_so(monkeypatch):
    """Not a silent fall-through to a client with no credentials."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _cfg(model_provider="anthropic-oauth", model_name="claude-opus-4-6")
    cfg.api_key = ""

    with pytest.raises(RuntimeError, match="no gateway key is configured"):
        create_llm(cfg, model_name="gateway:protolabs/coder")


def test_the_main_model_is_unaffected_by_the_grammar():
    """Only an explicit per-slot override routes elsewhere; `model.name` still belongs
    to `model.provider`."""
    llm = create_llm(_cfg())

    assert llm.model_name == "protolabs/reasoning"


def test_a_qualified_favorite_switches_the_chat_to_another_provider():
    """`model.favorites` (#1957) feeds the `/model` quick-switch, and the per-chat
    override resolves its selection through `create_llm`. So a favorite naming another
    provider is a cross-provider chat switch with no extra wiring — which is the point
    of putting the grammar in `create_llm` rather than in each slot's own resolver."""
    from graph.middleware.model_override import ModelOverrideMiddleware

    cfg = _cfg(model_favorites=["gateway:protolabs/coder", "openai-codex:gpt-5.6-sol"])
    mw = ModelOverrideMiddleware(cfg)

    codex = mw._llm_for("openai-codex:gpt-5.6-sol", "")
    gateway = mw._llm_for("gateway:protolabs/coder", "")

    assert "gpt-5.6-sol" in str(getattr(codex, "model", getattr(codex, "model_name", "")))
    assert "chatgpt.example" in str(getattr(codex, "openai_api_base", ""))
    assert gateway.model_name == "protolabs/coder"
    assert str(gateway.openai_api_base).rstrip("/") == _GATEWAY.rstrip("/")
