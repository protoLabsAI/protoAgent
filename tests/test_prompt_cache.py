"""Tests for PromptCacheMiddleware (Anthropic caching + context delivery)."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage

from graph.middleware.prompt_cache import PromptCacheMiddleware


class _Req:
    """Minimal stand-in for langchain's ModelRequest (the fields the
    middleware touches), with an override() that returns an updated copy."""

    def __init__(self, model_name, system_message, state=None):
        self.model = SimpleNamespace(model_name=model_name)
        self.system_message = system_message
        self.state = state or {}

    def override(self, **kw):
        r = _Req(self.model.model_name, self.system_message, self.state)
        for k, v in kw.items():
            setattr(r, k, v)
        return r


def _run(mw, req):
    captured = {}
    mw.wrap_model_call(req, lambda r: captured.setdefault("req", r) or "resp")
    return captured["req"]


def test_anthropic_caches_stable_prefix_and_delivers_context():
    mw = PromptCacheMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE PROMPT"), state={"context": "retrieved knowledge"})
    out = _run(mw, req)
    blocks = out.system_message.content
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == "STABLE PROMPT"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}  # cached prefix
    # context delivered AFTER the breakpoint, uncached
    assert "retrieved knowledge" in blocks[1]["text"]
    assert "cache_control" not in blocks[1]


def test_gateway_alias_gets_cache_blocks_by_default():
    # #2255: attempt-by-default — an alias like protolabs/* no longer silently
    # skips caching just because its NAME doesn't look Anthropic.
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/reasoning", SystemMessage(content="PROMPT"), state={"context": "knowledge here"})
    out = _run(mw, req)
    blocks = out.system_message.content
    assert isinstance(blocks, list)
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "knowledge here" in blocks[1]["text"]


def test_anthropic_no_context_caches_only():
    mw = PromptCacheMiddleware()
    req = _Req("claude-sonnet-4-6", SystemMessage(content="PROMPT"), state={})
    out = _run(mw, req)
    blocks = out.system_message.content
    assert len(blocks) == 1 and blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_noop_when_disabled_and_no_context():
    mw = PromptCacheMiddleware(enabled=False)
    sm = SystemMessage(content="PROMPT")
    req = _Req("gpt-5", sm, state={})
    out = _run(mw, req)
    assert out.system_message is sm  # unchanged — nothing to deliver, nothing to cache


def test_ttl_persistent_tier():
    mw = PromptCacheMiddleware(ttl="1h")
    req = _Req("claude-opus-4-7", SystemMessage(content="P"), state={})
    out = _run(mw, req)
    assert out.system_message.content[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_force_caches_non_anthropic():
    mw = PromptCacheMiddleware(force=True)
    req = _Req("protolabs/reasoning", SystemMessage(content="P"), state={})
    out = _run(mw, req)
    assert out.system_message.content[0]["cache_control"] == {"type": "ephemeral"}


def test_disabled_still_delivers_context_but_no_cache():
    mw = PromptCacheMiddleware(enabled=False)
    req = _Req("claude-opus-4-7", SystemMessage(content="P"), state={"context": "ctx"})
    out = _run(mw, req)
    assert isinstance(out.system_message.content, str)  # no cache blocks
    assert "ctx" in out.system_message.content


# ── fail-loud outcome watching (#2255) ─────────────────────────────────────────


def _usage_response(cache_read=0, cache_creation=0):
    from types import SimpleNamespace as NS

    from langchain_core.messages import AIMessage

    return NS(
        result=[
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 5,
                    "total_tokens": 105,
                    "input_token_details": {"cache_read": cache_read, "cache_creation": cache_creation},
                },
            )
        ]
    )


BIG = "S" * 5000  # over MIN_CACHEABLE_CHARS — zero cache activity is a real signal


def test_rejection_falls_back_to_plain_and_disables_model(caplog):
    # A provider that 400s on cache_control gets ONE retry without blocks, and
    # that model stays on plain delivery for the rest of the session.
    mw = PromptCacheMiddleware()
    seen = []

    def handler(r):
        seen.append(r.system_message.content)
        if isinstance(r.system_message.content, list):
            raise ValueError("provider rejected field: cache_control not supported")
        return _usage_response()

    req = _Req("protolabs/reasoning", SystemMessage(content=BIG), state={"context": "c"})
    with caplog.at_level("WARNING"):
        mw.wrap_model_call(req, handler)
    assert isinstance(seen[0], list) and isinstance(seen[1], str)  # blocks, then plain retry
    assert "rejected cache_control" in caplog.text

    seen.clear()
    mw.wrap_model_call(req, handler)
    assert len(seen) == 1 and isinstance(seen[0], str)  # goes straight to plain now


def test_unrelated_error_propagates_without_retry():
    mw = PromptCacheMiddleware()
    calls = []

    def handler(r):
        calls.append(1)
        raise ValueError("rate limited")

    req = _Req("protolabs/reasoning", SystemMessage(content=BIG), state={})
    with pytest.raises(ValueError, match="rate limited"):
        mw.wrap_model_call(req, handler)
    assert len(calls) == 1  # no blind retry


def test_force_never_falls_back(caplog):
    # force=True = the operator's word: a rejection propagates instead of degrading.
    mw = PromptCacheMiddleware(force=True)

    def handler(r):
        raise ValueError("cache_control not supported")

    req = _Req("whatever/alias", SystemMessage(content=BIG), state={})
    with pytest.raises(ValueError, match="cache_control"):
        mw.wrap_model_call(req, handler)


def test_zero_hit_streak_warns_once_per_model(caplog):
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/fast", SystemMessage(content=BIG), state={})
    with caplog.at_level("WARNING"):
        for _ in range(5):
            mw.wrap_model_call(req, lambda r: _usage_response())
    hits = [r for r in caplog.records if "ZERO cache activity" in r.message]
    assert len(hits) == 1  # fires at the threshold, then latches
    assert "protolabs/fast" in hits[0].message


def test_cache_hit_resets_the_streak(caplog):
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/fast", SystemMessage(content=BIG), state={})
    with caplog.at_level("WARNING"):
        for _ in range(2):
            mw.wrap_model_call(req, lambda r: _usage_response())
        mw.wrap_model_call(req, lambda r: _usage_response(cache_read=90))  # hit — reset
        for _ in range(2):
            mw.wrap_model_call(req, lambda r: _usage_response())
    assert "ZERO cache activity" not in caplog.text


def test_small_prefix_never_warns(caplog):
    # Under the provider's ~1024-token floor, zero activity is EXPECTED — silence.
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/fast", SystemMessage(content="tiny prompt"), state={"context": "c"})
    with caplog.at_level("WARNING"):
        for _ in range(5):
            mw.wrap_model_call(req, lambda r: _usage_response())
    assert "ZERO cache activity" not in caplog.text


def test_config_wires_middleware():
    from langchain.agents.middleware import AgentMiddleware

    from graph.config import LangGraphConfig
    from graph.agent import _build_middleware

    mw = _build_middleware(LangGraphConfig(), knowledge_store=None)
    names = [m.__class__.__name__ for m in mw]
    # ToolCallRepairMiddleware runs first — heal a dangling-tool_call history before
    # anything else touches it. WaitYieldMiddleware sits alongside it (both are
    # before_model-only gates that never wrap the model call).
    assert names[0] == "ToolCallRepairMiddleware"
    assert "WaitYieldMiddleware" in names
    # ModelOverrideMiddleware is the outermost *model* wrapper — it swaps the
    # per-tab model FIRST, so PromptCacheMiddleware (the next wrapper) sees the
    # ACTUAL model when deciding whether to cache. ModelOverride doesn't touch the
    # system message, so the cache breakpoint still lands on the stable prefix.
    wrappers = [m.__class__.__name__ for m in mw if type(m).wrap_model_call is not AgentMiddleware.wrap_model_call]
    assert wrappers[:2] == ["ModelOverrideMiddleware", "PromptCacheMiddleware"]


@pytest.mark.asyncio
async def test_async_path():
    mw = PromptCacheMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="P"), state={"context": "c"})
    captured = {}

    async def handler(r):
        captured["req"] = r
        return "resp"

    await mw.awrap_model_call(req, handler)
    assert captured["req"].system_message.content[0]["cache_control"]
