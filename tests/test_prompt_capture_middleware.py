"""PromptCaptureMiddleware (#2243) — snapshots the exact post-PromptCache system
prompt per model call, keyed by the request-context task id, best-effort always.

The chain in these tests mirrors production list order (PromptCache first,
capture directly after): capture's handler sees the request PromptCache built.
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, SystemMessage

from graph.middleware.prompt_cache import PromptCacheMiddleware
from graph.middleware.prompt_capture import PromptCaptureMiddleware
from graph.middleware.request_context import request_metadata_scope
from observability.prompt_snapshots import prompt_snapshots


class _Req:
    """Minimal ModelRequest stand-in (the test_prompt_cache shape)."""

    def __init__(self, model_name, system_message, state=None):
        self.model = SimpleNamespace(model_name=model_name)
        self.system_message = system_message
        self.state = state or {}

    def override(self, **kw):
        r = _Req(self.model.model_name, self.system_message, self.state)
        for k, v in kw.items():
            setattr(r, k, v)
        return r


def _response(usage=None):
    msg = AIMessage(content="ok", usage_metadata=usage)
    return SimpleNamespace(result=[msg])


def _run_chained(req, response, capture=None, cache=None):
    """Run PromptCache wrapping PromptCapture (production order) to a canned
    response; returns what the chain returned."""
    cache = cache or PromptCacheMiddleware()
    capture = capture or PromptCaptureMiddleware()
    return cache.wrap_model_call(req, lambda r: capture.wrap_model_call(r, lambda _r: response))


def test_captures_blocks_split_with_task_id_and_usage():
    # Anthropic path: PromptCache emits [stable(cache_control), tail] blocks —
    # capture stores them verbatim, keyed by the executor-threaded task id,
    # with the response's real usage.
    usage = {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "input_token_details": {"cache_read": 100, "cache_creation": 7},
    }
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={"context": "hot memory"})
    with request_metadata_scope({"a2a.task_id": "task-9"}):
        out = _run_chained(req, _response(usage))
    assert out.result[0].content == "ok"  # the response passes through untouched

    calls = prompt_snapshots().calls_for_task("task-9")
    assert len(calls) == 1
    row = calls[0]
    assert row["stable_text"] == "STABLE"
    assert "hot memory" in row["context_text"]
    # stable + tail is byte-for-byte what the model received.
    assert row["context_text"].startswith("\n\n# Context\n\n")
    assert row["model"] == "claude-opus-4-7"
    assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
    assert (row["cache_read_tokens"], row["cache_creation_tokens"]) == (100, 7)


def test_captures_plain_string_split_when_cache_disabled():
    # Caching off (disabled, or a model that rejected blocks — #2255): PromptCache
    # appends the tail as plain text — capture recovers the same stable/tail split
    # by matching the exact suffix.
    req = _Req("protolabs/reasoning", SystemMessage(content="STABLE"), state={"context": "ctx"})
    with request_metadata_scope({"a2a.task_id": "task-str"}):
        _run_chained(req, _response(), cache=PromptCacheMiddleware(enabled=False))
    row = prompt_snapshots().calls_for_task("task-str")[0]
    assert row["stable_text"] == "STABLE"
    assert row["context_text"] == "\n\n# Context\n\nctx"


def test_captures_untouched_prompt_when_cache_noops():
    # No context + caching disabled → PromptCache passes the request through; the
    # snapshot is the plain system prompt with an empty tail.
    req = _Req("protolabs/reasoning", SystemMessage(content="JUST-STABLE"), state={})
    with request_metadata_scope({"a2a.task_id": "task-plain"}):
        _run_chained(req, _response(), cache=PromptCacheMiddleware(enabled=False))
    row = prompt_snapshots().calls_for_task("task-plain")[0]
    assert row["stable_text"] == "JUST-STABLE"
    assert row["context_text"] == ""


def test_no_system_message_captures_nothing():
    req = _Req("claude-opus-4-7", None, state={})
    with request_metadata_scope({"a2a.task_id": "task-none"}):
        _run_chained(req, _response())
    assert prompt_snapshots().calls_for_task("task-none") == []


def test_incognito_turn_captures_nothing():
    # Incognito = no durable trail (ADR 0069 D3b); a persisted prompt snapshot
    # would be one.
    req = _Req("claude-opus-4-7", SystemMessage(content="S"), state={"incognito": True, "context": "c"})
    with request_metadata_scope({"a2a.task_id": "task-incog"}):
        _run_chained(req, _response())
    assert prompt_snapshots().calls_for_task("task-incog") == []


def test_store_failure_never_touches_the_turn(monkeypatch):
    # Best-effort like the injection log: a broken store debug-logs; the
    # response still comes back.
    capture = PromptCaptureMiddleware()

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(capture, "_store", _boom)
    req = _Req("claude-opus-4-7", SystemMessage(content="S"), state={"context": "c"})
    out = _run_chained(req, _response(), capture=capture)
    assert out.result[0].content == "ok"


def test_async_twin_captures():
    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="ASYNC-STABLE"), state={"context": "c"})
    cache = PromptCacheMiddleware()

    async def _go():
        async def _handler(_r):
            return _response()

        async def _inner(r):
            return await capture.awrap_model_call(r, _handler)

        with request_metadata_scope({"a2a.task_id": "task-async"}):
            return await cache.awrap_model_call(req, _inner)

    asyncio.run(_go())
    row = prompt_snapshots().calls_for_task("task-async")[0]
    assert row["stable_text"] == "ASYNC-STABLE"


def test_retention_knob_reaches_the_store():
    capture = PromptCaptureMiddleware(retention_days=7)
    req = _Req("claude-opus-4-7", SystemMessage(content="S"), state={})
    with request_metadata_scope({"a2a.task_id": "t"}):
        _run_chained(req, _response(), capture=capture)
    assert prompt_snapshots().retention_days == 7


# --- middleware-order contract -----------------------------------------------


def test_capture_sits_directly_after_prompt_cache():
    # The ordering IS the correctness: capture must see the final assembled
    # system message, which only exists inside PromptCache's wrap.
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig

    names = [type(m).__name__ for m in _build_middleware(LangGraphConfig(api_key="k"), None)]
    assert names.index("PromptCaptureMiddleware") == names.index("PromptCacheMiddleware") + 1


def test_capture_absent_when_disabled():
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig(api_key="k", prompt_capture_enabled=False)
    names = [type(m).__name__ for m in _build_middleware(cfg, None)]
    assert "PromptCaptureMiddleware" not in names
