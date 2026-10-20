"""anthropic-oauth sends the thinking/effort shape each Claude family accepts (#4418).

Claude 4.6+ (and every 5.x model) take adaptive thinking + ``output_config.effort``;
``budget_tokens`` is a 400 on Opus 5.5 (and langchain-anthropic refuses it client-side
for the 5 family). Models before 4.6 only take the manual ``budget_tokens`` shape.
These build the real ``_OAuthChatAnthropic`` and read the request payload it would send.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from graph.config import LangGraphConfig
from graph.providers import oauth as oauth_mod
from graph.providers.anthropic_oauth import build_anthropic_oauth_llm, claude_thinking_kwargs

pytestmark = pytest.mark.platform_sensitive

ADAPTIVE = {"type": "adaptive", "display": "summarized"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(oauth_mod, "_read_claude_keychain", lambda: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-test")


def _payload(model: str, *, thinking: str = "", effort: str | None = None, max_tokens: int = 32000) -> dict:
    cfg = LangGraphConfig(
        model_provider="anthropic-oauth",
        model_name=model,
        thinking=thinking,
        reasoning_effort=effort,
        max_tokens=max_tokens,
    )
    llm = build_anthropic_oauth_llm(cfg)
    return llm._get_request_payload([HumanMessage("hi")])


# ── adaptive families: 5.x, Fable, 4.6–4.8 ────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-haiku-5-5",
        "claude-fable-5-1",
        "claude-opus-4-8",
        "claude-opus-4-7",
    ],
)
def test_current_models_get_adaptive_thinking_and_effort_never_budget(model):
    body = _payload(model, effort="high")
    assert body["thinking"] == ADAPTIVE
    assert body["output_config"]["effort"] == "high"
    assert "budget_tokens" not in body["thinking"]


def test_opus_55_thinking_enabled_without_effort_is_adaptive():
    body = _payload("claude-opus-5-5", thinking="enabled")
    assert body["thinking"] == ADAPTIVE
    assert "effort" not in (body.get("output_config") or {})


def test_opus_55_nothing_set_sends_neither():
    body = _payload("claude-opus-5-5")
    assert "thinking" not in body
    assert "effort" not in (body.get("output_config") or {})


def test_thinking_disabled_sends_effort_only():
    # Opus 5.5 400s on an explicit {type: disabled}; effort is the only lever there.
    body = _payload("claude-opus-5-5", thinking="disabled", effort="low")
    assert "thinking" not in body
    assert body["output_config"]["effort"] == "low"


@pytest.mark.parametrize("model", ["claude-opus-4-6", "claude-sonnet-4-6"])
def test_46_family_is_adaptive_and_xhigh_steps_down(model):
    body = _payload(model, effort="xhigh")
    assert body["thinking"] == ADAPTIVE
    assert body["output_config"]["effort"] == "high"  # xhigh arrived with 4.7


def test_xhigh_passes_through_on_47_plus():
    assert _payload("claude-opus-5-5", effort="xhigh")["output_config"]["effort"] == "xhigh"


def test_unknown_future_model_defaults_to_adaptive():
    assert claude_thinking_kwargs("claude-opus-7", effort="max") == {
        "thinking": ADAPTIVE,
        "output_config": {"effort": "max"},
    }


def test_unknown_effort_is_dropped_not_sent():
    assert claude_thinking_kwargs("claude-opus-5-5", effort="turbo") == {}
    assert claude_thinking_kwargs("claude-opus-5-5", effort="minimal")["output_config"] == {"effort": "low"}


# ── legacy families: manual budget_tokens ─────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-5",
        "claude-opus-4-1",
        "claude-sonnet-4-20250514",
        "claude-3-7-sonnet-latest",
    ],
)
def test_pre_46_models_keep_budget_tokens(model):
    body = _payload(model, effort="high")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert "effort" not in (body.get("output_config") or {})


def test_legacy_budget_scales_with_effort_and_stays_under_max_tokens():
    assert _payload("claude-haiku-4-5", thinking="enabled")["thinking"]["budget_tokens"] == 8192
    assert _payload("claude-haiku-4-5", effort="low")["thinking"]["budget_tokens"] == 4096
    assert _payload("claude-haiku-4-5", effort="max", max_tokens=8000)["thinking"]["budget_tokens"] == 7999
    # Too small a max_tokens for any legal budget: send no thinking rather than a 400.
    assert "thinking" not in _payload("claude-haiku-4-5", effort="high", max_tokens=1024)


def test_legacy_thinking_disabled_sends_nothing():
    assert claude_thinking_kwargs("claude-sonnet-4-5", thinking="disabled", effort="high") == {}
