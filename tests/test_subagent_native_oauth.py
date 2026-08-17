"""Native-OAuth subagents (bd-wmz, follow-up to #2519/#2545).

A Claude/ChatGPT-subscription instance could CHAT while every delegation
failed: the subagent stack built its own middleware list and carried neither
provider-shape transform, so it emitted a system-role input item (which the
Codex Responses backend rejects outright) or traffic with no Claude Code
identity line (which Anthropic's OAuth infra refuses). Both stacks now share
``provider_shape_middleware``; these pin that they stay in step.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import graph.agent as agent_mod
from graph.agent import provider_shape_middleware
from graph.config import LangGraphConfig


def _names(mws) -> list[str]:
    return [type(m).__name__ for m in mws]


# ── the shared helper ─────────────────────────────────────────────────────────


def test_helper_shapes_per_provider():
    def names(provider, capture=False):
        # capture defaults ON in real config; pin it explicitly so this test is
        # about the PROVIDER shape, not the observer's gate.
        cfg = LangGraphConfig(model_provider=provider, prompt_capture_enabled=capture)
        return _names(provider_shape_middleware(cfg))

    assert names("anthropic-oauth") == ["ClaudeCodeIdentityMiddleware"]
    assert names("openai-codex") == ["CodexResponsesInputMiddleware"]
    assert names("openai") == []  # gateway: nothing to reshape
    # The wire observer trails the transform (#2527) — never precedes it, or it
    # would record the composed prompt as if it were what the wire carried.
    assert names("openai-codex", capture=True) == [
        "CodexResponsesInputMiddleware",
        "WirePromptCaptureMiddleware",
    ]


def test_helper_is_case_and_whitespace_tolerant():
    cfg = LangGraphConfig(model_provider="  OpenAI-Codex  ", prompt_capture_enabled=False)
    assert _names(provider_shape_middleware(cfg)) == ["CodexResponsesInputMiddleware"]


# ── the subagent stack actually mounts them ───────────────────────────────────


def _capture_subagent_middleware(monkeypatch, provider: str) -> list[str]:
    """Drive _run_subagent far enough to see the middleware list it hands to
    create_agent, without a model or a real graph."""
    seen: dict = {}

    def fake_create_agent(**kwargs):
        seen["middleware"] = kwargs.get("middleware") or []

        class _Agent:
            async def ainvoke(self, *_a, **_kw):
                return {"messages": [SimpleNamespace(content="ok", type="ai")]}

        return _Agent()

    monkeypatch.setattr(agent_mod, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_kw: object())

    cfg = LangGraphConfig(model_provider=provider)
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
    return _names(seen.get("middleware", []))


def test_codex_subagent_carries_the_responses_transform(monkeypatch):
    """WITHOUT this the Codex backend rejects the delegation's system-role item
    ('System messages are not allowed') — chat worked, every task() failed."""
    names = _capture_subagent_middleware(monkeypatch, "openai-codex")
    assert "CodexResponsesInputMiddleware" in names, names
    # Innermost: nothing that touches the system message may run after it, and it
    # must sit INSIDE PromptCapture (which records the composed prompt).
    if "PromptCaptureMiddleware" in names:
        assert names.index("PromptCaptureMiddleware") < names.index("CodexResponsesInputMiddleware")


def test_claude_subagent_carries_the_identity_prepend(monkeypatch):
    """Anthropic's OAuth infra refuses traffic whose system prompt doesn't lead
    with the Claude Code identity line."""
    names = _capture_subagent_middleware(monkeypatch, "anthropic-oauth")
    assert "ClaudeCodeIdentityMiddleware" in names, names


def test_gateway_subagent_is_unchanged(monkeypatch):
    """No native provider ⇒ no reshaping (and no behavior change for gateways)."""
    names = _capture_subagent_middleware(monkeypatch, "openai")
    assert "CodexResponsesInputMiddleware" not in names
    assert "ClaudeCodeIdentityMiddleware" not in names


def test_subagent_stack_carries_prompt_cache(monkeypatch):
    """#2778 / ADR 0101 D1: a subagent's system prompt is static per build, so
    every delegation paying full uncached input on it was pure waste — the stack
    now mirrors the lead's PromptCacheMiddleware (same knobs, same watchers)."""
    names = _capture_subagent_middleware(monkeypatch, "openai")
    assert "PromptCacheMiddleware" in names, names


def test_subagent_prompt_cache_precedes_the_identity_shape(monkeypatch):
    """Ordering mirrors the lead stack: the provider-shape transform stays
    innermost/last (it must see the FINAL system message, cache blocks included —
    it prepends the identity line onto the block list, which is exactly how the
    lead path composes with caching)."""
    names = _capture_subagent_middleware(monkeypatch, "anthropic-oauth")
    assert "PromptCacheMiddleware" in names, names
    assert names.index("PromptCacheMiddleware") < names.index("ClaudeCodeIdentityMiddleware")
