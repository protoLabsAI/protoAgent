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
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE PROMPT"),
               state={"context": "retrieved knowledge"})
    out = _run(mw, req)
    blocks = out.system_message.content
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == "STABLE PROMPT"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}  # cached prefix
    # context delivered AFTER the breakpoint, uncached
    assert "retrieved knowledge" in blocks[1]["text"]
    assert "cache_control" not in blocks[1]


def test_non_anthropic_delivers_context_without_cache_control():
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/reasoning", SystemMessage(content="PROMPT"),
               state={"context": "knowledge here"})
    out = _run(mw, req)
    # plain string append, no cache_control blocks (safe for any provider)
    assert isinstance(out.system_message.content, str)
    assert "PROMPT" in out.system_message.content
    assert "knowledge here" in out.system_message.content


def test_anthropic_no_context_caches_only():
    mw = PromptCacheMiddleware()
    req = _Req("claude-sonnet-4-6", SystemMessage(content="PROMPT"), state={})
    out = _run(mw, req)
    blocks = out.system_message.content
    assert len(blocks) == 1 and blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_noop_when_no_context_and_not_cacheable():
    mw = PromptCacheMiddleware()
    sm = SystemMessage(content="PROMPT")
    req = _Req("gpt-5", sm, state={})
    out = _run(mw, req)
    assert out.system_message is sm  # unchanged


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
    wrappers = [
        m.__class__.__name__
        for m in mw
        if type(m).wrap_model_call is not AgentMiddleware.wrap_model_call
    ]
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
