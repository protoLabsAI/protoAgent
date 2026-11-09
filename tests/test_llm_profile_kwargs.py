"""ADR 0128 S2a — create_llm(profile=...) applied as a dataclasses.replace config view.

Two properties:
  * with no profile the built kwargs are byte-identical to today's, for the gateway,
    anthropic-oauth and codex builders (characterization);
  * a gateway ``routing`` profile caps max_tokens and turns thinking off through both
    extra_body.thinking and chat_template_kwargs.enable_thinking, and a knob a provider
    type ignores is logged once per (provider type, knob).
"""

import logging
from types import SimpleNamespace

import graph.llm as llm
from graph.config import GenerationProfile, LangGraphConfig
from graph.generation_profile import ResolvedProfile, resolve_profile


def _gateway_kwargs(monkeypatch, cfg, **create_kwargs) -> dict:
    captured: dict = {}

    class Fake:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(llm, "_ReasoningChatOpenAI", Fake)
    llm.create_llm(cfg, **create_kwargs)
    return captured


def _anthropic_kwargs(monkeypatch, cfg, **create_kwargs) -> dict:
    import graph.providers.anthropic_oauth as ao

    captured: dict = {}

    class Fake:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(ao, "_OAuthChatAnthropic", Fake)
    monkeypatch.setattr(ao, "resolve_anthropic_oauth", lambda: SimpleNamespace(access_token="tok"))
    llm.create_llm(cfg, **create_kwargs)
    return captured


def _codex_kwargs(monkeypatch, cfg, **create_kwargs) -> dict:
    import graph.providers.codex_client as codex_client
    import graph.providers.openai_codex as codex_mod
    from graph.providers.oauth import CodexOAuthCreds

    captured: dict = {}

    def _fake_build(*, issuer, **kw):
        captured.update(kw)
        captured["issuer"] = issuer
        return SimpleNamespace()

    monkeypatch.setattr(
        codex_mod,
        "resolve_codex_oauth",
        lambda: CodexOAuthCreds(
            access_token="tok",
            account_id="acct",
            base_url="https://chatgpt.com/backend-api/codex",
            source="instance_store",
        ),
    )
    monkeypatch.setattr(codex_client, "build_codex_client", _fake_build)
    llm.create_llm(cfg, **create_kwargs)
    return captured


_GATEWAY_CFG = dict(api_base="http://gw.test/v1", api_key="sk")
_ANTHROPIC_CFG = dict(model_provider="anthropic-oauth", model_name="claude-sonnet-5")
_CODEX_CFG = dict(model_provider="openai-codex", model_name="gpt-5-codex")


def test_no_profile_is_byte_identical_on_the_gateway(monkeypatch):
    cfg = LangGraphConfig(**_GATEWAY_CFG)
    base = _gateway_kwargs(monkeypatch, cfg)  # today's call signature, no profile kwarg
    assert _gateway_kwargs(monkeypatch, cfg, profile=None) == base
    assert _gateway_kwargs(monkeypatch, cfg, profile=ResolvedProfile()) == base


def test_no_profile_is_byte_identical_for_anthropic_oauth(monkeypatch):
    cfg = LangGraphConfig(**_ANTHROPIC_CFG)
    base = _anthropic_kwargs(monkeypatch, cfg)
    assert base  # the builder ran
    assert _anthropic_kwargs(monkeypatch, cfg, profile=None) == base
    assert _anthropic_kwargs(monkeypatch, cfg, profile=ResolvedProfile()) == base


def test_no_profile_is_byte_identical_for_codex(monkeypatch):
    cfg = LangGraphConfig(**_CODEX_CFG)
    base = _codex_kwargs(monkeypatch, cfg)
    assert base
    assert _codex_kwargs(monkeypatch, cfg, profile=None) == base
    assert _codex_kwargs(monkeypatch, cfg, profile=ResolvedProfile()) == base


def test_gateway_routing_profile_caps_tokens_and_disables_thinking(monkeypatch):
    cfg = LangGraphConfig(
        **_GATEWAY_CFG,
        model_profiles={"routing": GenerationProfile(max_tokens=4096, thinking="disabled")},
    )
    kw = _gateway_kwargs(monkeypatch, cfg, profile=resolve_profile(cfg, call="routing"))
    assert kw["max_tokens"] == 4096
    assert kw["extra_body"]["thinking"] == {"type": "disabled"}
    assert kw["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_ignored_knob_logs_once_per_provider_and_knob(monkeypatch, caplog):
    # The gateway honours max_tokens but has no thinking_budget wire (ADR 0128 D5), so a
    # profile that sets both reports only thinking_budget, and only once across two builds.
    llm._PROFILE_IGNORE_LOGGED.clear()
    cfg = LangGraphConfig(
        **_GATEWAY_CFG,
        model_profiles={"cap": GenerationProfile(thinking_budget=2048, max_tokens=4096)},
    )
    prof = resolve_profile(cfg, call="cap")
    with caplog.at_level(logging.INFO, logger="graph.llm"):
        _gateway_kwargs(monkeypatch, cfg, profile=prof)
        _gateway_kwargs(monkeypatch, cfg, profile=prof)  # second build must not re-log
    fired = [r for r in caplog.records if "does not apply" in r.getMessage()]
    assert len(fired) == 1
    assert "thinking_budget" in fired[0].getMessage()
