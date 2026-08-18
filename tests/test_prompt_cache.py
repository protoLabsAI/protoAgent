"""Tests for PromptCacheMiddleware (Anthropic caching + context delivery)."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage

from graph.middleware.prompt_cache import PromptCacheMiddleware


class _Req:
    """Minimal stand-in for langchain's ModelRequest (the fields the
    middleware touches), with an override() that returns an updated copy."""

    def __init__(self, model_name, system_message, state=None, model=None, messages=None):
        self.model = model if model is not None else SimpleNamespace(model_name=model_name)
        self.system_message = system_message
        self.state = state or {}
        self.messages = list(messages or [])

    def override(self, **kw):
        r = _Req("", self.system_message, self.state, model=self.model, messages=self.messages)
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


def _unreported_usage_response():
    """OpenAI-compat provider WITHOUT cache reporting: no cache keys at all
    (langchain-openai filters the Nones a null prompt_tokens_details produces)."""
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
                    "input_token_details": {},
                },
            )
        ]
    )


def test_unreported_usage_warns_reporting_gap_not_ignoring(caplog):
    # homelab-iac#242: a lane can cache invisibly. Absent cache fields must not
    # be accused of "ignoring prompt caching" — that message sends the operator
    # to the wrong fix (disabling blocks) when the real fix is the lane's
    # reporting flag.
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/reasoning", SystemMessage(content=BIG), state={})
    with caplog.at_level("WARNING"):
        for _ in range(5):
            mw.wrap_model_call(req, lambda r: _unreported_usage_response())
    hits = [r for r in caplog.records if "NO cache-usage fields" in r.message]
    assert len(hits) == 1  # fires at the threshold, then latches
    assert "enable-prompt-tokens-details" in hits[0].message
    assert "ZERO cache activity" not in caplog.text  # not the ignoring message


def test_reported_zero_still_warns_ignoring(caplog):
    # Explicit zeros = the provider reports and there was genuinely no cache
    # activity — the original "likely ignoring" message stays for that case.
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/fast", SystemMessage(content=BIG), state={})
    with caplog.at_level("WARNING"):
        for _ in range(3):
            mw.wrap_model_call(req, lambda r: _usage_response())
    assert "ZERO cache activity" in caplog.text
    assert "NO cache-usage fields" not in caplog.text


def test_cache_hit_resets_reported_flavor(caplog):
    # A hit clears both the streak and its reported/unreported flavor, so a
    # later streak is judged only on its own calls.
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/fast", SystemMessage(content=BIG), state={})
    with caplog.at_level("WARNING"):
        mw.wrap_model_call(req, lambda r: _usage_response())  # reported zero
        mw.wrap_model_call(req, lambda r: _usage_response(cache_read=90))  # hit — reset
        for _ in range(3):
            mw.wrap_model_call(req, lambda r: _unreported_usage_response())
    assert "NO cache-usage fields" in caplog.text  # judged as unreported, not ignoring
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
    # Trajectory sits BETWEEN them by design (ADR 0102 S1): inside ModelOverride
    # (logs the real per-tab model) but outside PromptCache (its refs must hash
    # the STORED message bytes, not the view-only cache-marked copies).
    assert wrappers[:3] == ["ModelOverrideMiddleware", "TrajectoryMiddleware", "PromptCacheMiddleware"]


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


# ── rolling history breakpoints (#2777, ADR 0101 D1) ─────────────────────────


def _hist_req(model_name="claude-opus-4-7", messages=None, model=None):
    return _Req(model_name, SystemMessage(content="STABLE"), state={}, model=model, messages=messages)


def test_history_marks_last_two_markable_messages():
    from langchain_core.messages import AIMessage, HumanMessage

    mw = PromptCacheMiddleware()
    msgs = [HumanMessage(content="q1"), AIMessage(content="a1"), HumanMessage(content="q2"), AIMessage(content="a2")]
    out = _run(mw, _hist_req(messages=msgs))
    marked = [m for m in out.messages if isinstance(m.content, list) and "cache_control" in m.content[-1]]
    assert len(marked) == 2
    # The newest two, walking from the tail.
    assert marked[0].content[0]["text"] == "q2"
    assert marked[1].content[0]["text"] == "a2"
    # View-only: the ORIGINAL stored messages are untouched strings.
    assert all(isinstance(m.content, str) for m in msgs)


def test_history_skips_tool_messages_on_the_gateway_path():
    """langchain-openai's tool converter drops cache_control — marking a
    ToolMessage there would waste a slot on a silent no-op. The walk skips them
    and marks the newest human/assistant text instead."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    mw = PromptCacheMiddleware()
    msgs = [
        HumanMessage(content="q"),
        AIMessage(content="calling", tool_calls=[{"name": "t", "args": {}, "id": "x", "type": "tool_call"}]),
        ToolMessage(content="result", tool_call_id="x"),
    ]
    out = _run(mw, _hist_req("protolabs/reasoning", messages=msgs))
    assert isinstance(out.messages[2].content, str)  # tool result untouched
    assert out.messages[1].content[-1]["cache_control"]  # AI text marked
    assert out.messages[0].content[-1]["cache_control"]  # human marked


def test_history_marks_tool_results_on_the_native_anthropic_client():
    """langchain-anthropic lifts a block's cache_control onto the tool_result
    envelope — so on ChatAnthropic (incl. the oauth subclass) the newest tool
    results ARE markable, zero-lag caching for tool-heavy rounds."""
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, ToolMessage

    mw = PromptCacheMiddleware()
    client = ChatAnthropic(model="claude-3-5-haiku-latest", api_key="test")
    msgs = [HumanMessage(content="q"), ToolMessage(content="big result", tool_call_id="x")]
    out = _run(mw, _hist_req(model=client, messages=msgs))
    assert out.messages[1].content[-1]["cache_control"]
    assert out.messages[0].content[-1]["cache_control"]


def test_history_skips_unmarkable_tails():
    """Empty tool-calls-only assistant steps and trailing non-text blocks have no
    legal breakpoint slot — the walk passes over them without spending budget."""
    from langchain_core.messages import AIMessage, HumanMessage

    mw = PromptCacheMiddleware()
    msgs = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "x", "type": "tool_call"}]),
        AIMessage(content=[{"type": "thinking", "thinking": "…"}]),
    ]
    out = _run(mw, _hist_req(messages=msgs))
    assert out.messages[0].content[-1]["cache_control"]  # only the human is markable
    assert out.messages[1].content == ""
    assert "cache_control" not in out.messages[2].content[-1]


def test_history_unmarked_when_caching_disabled():
    from langchain_core.messages import HumanMessage

    mw = PromptCacheMiddleware(enabled=False)
    req = _hist_req(messages=[HumanMessage(content="q")])
    req.state = {"context": "ctx"}  # forces the plain-delivery path to run
    out = _run(mw, req)
    assert isinstance(out.messages[0].content, str)


def test_history_marks_carry_the_ttl():
    from langchain_core.messages import HumanMessage

    mw = PromptCacheMiddleware(ttl="1h")
    out = _run(mw, _hist_req(messages=[HumanMessage(content="q")]))
    assert out.messages[0].content[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
