"""PromptCaptureMiddleware (#2243) — snapshots the exact post-PromptCache system
prompt per model call, keyed by the request-context task id, best-effort always.

The chain in these tests mirrors production list order (PromptCache first,
capture directly after): capture's handler sees the request PromptCache built.
"""

import asyncio
import contextlib
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
    # Anthropic path: PromptCache emits [stable(cache_control)] blocks —
    # capture stores them verbatim, keyed by the executor-threaded task id,
    # with the response's real usage. Context delivery is now ephemeral
    # (ADR 0108 D2) and does not appear in context_text.
    usage = {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "input_token_details": {"cache_read": 100, "cache_creation": 7},
    }
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"))
    with request_metadata_scope({"a2a.task_id": "task-9"}):
        out = _run_chained(req, _response(usage))
    assert out.result[0].content == "ok"  # the response passes through untouched

    calls = prompt_snapshots().calls_for_task("task-9")
    assert len(calls) == 1
    row = calls[0]
    assert row["stable_text"] == "STABLE"
    assert row["context_text"] == ""
    assert row["model"] == "claude-opus-4-7"
    assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
    assert (row["cache_read_tokens"], row["cache_creation_tokens"]) == (100, 7)


def test_captures_stable_only_when_cache_disabled():
    # Caching off (disabled, or a model that rejected blocks — #2255): PromptCache
    # passes the request through; the snapshot is the plain system prompt with an
    # empty tail (context delivery is now ephemeral, ADR 0108 D2).
    req = _Req("protolabs/reasoning", SystemMessage(content="STABLE"))
    with request_metadata_scope({"a2a.task_id": "task-str"}):
        _run_chained(req, _response(), cache=PromptCacheMiddleware(enabled=False))
    row = prompt_snapshots().calls_for_task("task-str")[0]
    assert row["stable_text"] == "STABLE"
    assert row["context_text"] == ""


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


def test_max_calls_knob_reaches_the_store_and_actually_evicts():
    # #3019: the row cap is what governs retention at real volume, so it must
    # travel the same path retention_days does — and the proof is EVICTION, not
    # an attribute set on the store.
    capture = PromptCaptureMiddleware(retention_days=0, max_calls=2)
    for i in range(3):
        req = _Req("claude-opus-4-7", SystemMessage(content="S"), state={})
        with request_metadata_scope({"a2a.task_id": f"t{i}"}):
            _run_chained(req, _response(), capture=capture)
    store = prompt_snapshots()
    assert store.max_calls == 2
    assert store.calls_for_task("t0") == []  # oldest dropped by the row cap
    assert len(store.calls_for_task("t2")) == 1
    assert store.retention_stats()["binding_cap"] == "max_calls"


def test_both_caps_travel_from_config_through_the_graph_build():
    # The whole delivery path in one assertion: prompts.max_calls on the config →
    # _build_middleware → PromptCaptureMiddleware → the live store. retention_days
    # was already wired; max_calls was the half that never arrived (#3019).
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig(api_key="k", prompt_capture_retention_days=11, prompt_capture_max_calls=3)
    capture = next(m for m in _build_middleware(cfg, None) if type(m).__name__ == "PromptCaptureMiddleware")
    req = _Req("claude-opus-4-7", SystemMessage(content="S"), state={})
    with request_metadata_scope({"a2a.task_id": "t"}):
        _run_chained(req, _response(), capture=capture)
    store = prompt_snapshots()
    assert (store.retention_days, store.max_calls) == (11, 3)


def test_the_subagent_stack_carries_the_row_cap_too(monkeypatch):
    """#2388's subagent stack builds its OWN middleware list, so a cap wired only
    into `_build_middleware` would leave every delegation writing at the
    hardcoded default — and delegation-heavy turns are precisely where the row
    cap fills fastest (#3019). Proof is eviction through the middleware the
    subagent path actually constructed, not the argument it was handed."""
    import graph.agent as agent_mod
    from graph.config import LangGraphConfig

    seen: dict = {}

    def fake_create_agent(**kwargs):
        seen["middleware"] = kwargs.get("middleware") or []

        class _Agent:
            async def ainvoke(self, *_a, **_kw):
                return {"messages": [SimpleNamespace(content="ok", type="ai")]}

        return _Agent()

    monkeypatch.setattr(agent_mod, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_kw: object())
    cfg = LangGraphConfig(api_key="k", prompt_capture_retention_days=0, prompt_capture_max_calls=1)
    # The run dies past create_agent on the fake agent (no astream) — the
    # test_subagent_native_oauth harness does the same. All we need is the
    # middleware list the real code path built, and the assert below fails
    # loudly if construction never got that far.
    with contextlib.suppress(Exception):
        asyncio.run(
            agent_mod._run_subagent(
                config=cfg,
                tool_map={"current_time": SimpleNamespace(name="current_time")},
                available_subagents="researcher",
                prompt="go",
                subagent_type="researcher",
                description="delegation under test",
                parent_task_id="call-xyz",
            )
        )
    capture = next(m for m in seen["middleware"] if type(m).__name__ == "PromptCaptureMiddleware")
    for _ in range(2):
        _run_chained(_Req("claude-opus-4-7", SystemMessage(content="S"), state={}), _response(), capture=capture)
    rows = prompt_snapshots().calls_for_parent("call-xyz")
    assert [r["call_index"] for r in rows] == [1]  # max_calls=1 → only the newest survives


# --- middleware-order contract -----------------------------------------------


def test_capture_sees_the_final_prompt():
    # The ordering IS the correctness: capture must see the final assembled system
    # message, which only exists inside PromptCache's wrap — and inside every other
    # prompt MUTATOR. Strict cache→capture adjacency was the old proxy for this;
    # RoomCast (#3049) now sits between them, appending the cast suffix, and capture
    # must record the prompt WITH it — a snapshot missing a block the model saw lies.
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig

    names = [type(m).__name__ for m in _build_middleware(LangGraphConfig(api_key="k"), None)]
    capture = names.index("PromptCaptureMiddleware")
    assert capture > names.index("PromptCacheMiddleware")
    assert capture > names.index("RoomCastMiddleware")
    # And nothing downstream of capture mutates the prompt — it stays the last
    # prompt-touching wrapper.
    assert "RoomCastMiddleware" not in names[capture + 1 :]


def test_capture_absent_when_disabled():
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig(api_key="k", prompt_capture_enabled=False)
    names = [type(m).__name__ for m in _build_middleware(cfg, None)]
    assert "PromptCaptureMiddleware" not in names


def test_subagent_identity_nests_rows_under_parent():
    """#2388 P3: a subagent-built capture claims NO task_id — even with a turn's
    a2a.task_id in scope, its rows nest under the delegating tool-call id + type
    (its own call_index space), so the main-loop tabs stay uncontaminated."""
    req = _Req("claude-opus-4-7", SystemMessage(content="SUB PROMPT"))
    capture = PromptCaptureMiddleware(
        stable_sections=[{"label": "researcher system prompt", "chars": 10}],
        parent_task_id="call-xyz",
        subagent_type="researcher",
    )
    with request_metadata_scope({"a2a.task_id": "task-77"}):
        _run_chained(req, _response(), capture=capture)

    assert prompt_snapshots().calls_for_task("task-77") == []
    subs = prompt_snapshots().calls_for_parent("call-xyz")
    assert len(subs) == 1
    row = subs[0]
    assert (row["task_id"], row["parent_task_id"], row["subagent_type"]) == ("", "call-xyz", "researcher")
    assert row["stable_text"] == "SUB PROMPT"
    assert row["stable_sections"] == [{"label": "researcher system prompt", "chars": 10}]


# ── wire-vs-composed capture (#2527) ──────────────────────────────────────────


def _run_wire_chain(req, response, *inner_mws):
    """Production order: capture OUTER, provider transforms inner, wire observer
    INNERMOST — the arrangement whose divergence #2527 makes visible."""
    from graph.middleware.wire_capture import WirePromptCaptureMiddleware

    capture = PromptCaptureMiddleware()

    def call(r, mws):
        if not mws:
            return WirePromptCaptureMiddleware().wrap_model_call(r, lambda _r: response)
        return mws[0].wrap_model_call(r, lambda r2: call(r2, mws[1:]))

    return capture.wrap_model_call(req, lambda r: call(r, list(inner_mws)))


def _row(task_id):
    rows = prompt_snapshots().calls_for_task(task_id)
    assert len(rows) == 1
    return rows[0]


def test_wire_faithful_delivery_records_null():
    """Gateway path, no transforms: wire == composed → wire_text NULL."""
    req = _Req("gpt-x", SystemMessage(content="STABLE"))
    with request_metadata_scope({"a2a.task_id": "wire-1"}):
        _run_wire_chain(req, _response())
    assert _row("wire-1")["wire_text"] is None


def test_wire_codex_instructions_move_is_faithful():
    """The (fixed, #2526) Codex transform moves the prompt verbatim into
    model_settings.instructions — content-faithful, so wire_text stays NULL."""
    from graph.middleware.codex_responses_input import CodexResponsesInputMiddleware

    req = _Req("gpt-5-codex", SystemMessage(content="STABLE"))
    req.model_settings = {}
    with request_metadata_scope({"a2a.task_id": "wire-2"}):
        _run_wire_chain(req, _response(), CodexResponsesInputMiddleware())
    assert _row("wire-2")["wire_text"] is None


def test_wire_claude_prepend_is_recorded():
    """The Claude identity prepend changes what the wire carries — the divergence
    is recorded so View prompt can show what was actually delivered."""
    from graph.middleware.claude_code_identity import ClaudeCodeIdentityMiddleware
    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    req = _Req("claude-opus-4-7", SystemMessage(content="You are Aria."))
    with request_metadata_scope({"a2a.task_id": "wire-3"}):
        _run_wire_chain(req, _response(), ClaudeCodeIdentityMiddleware())
    wire = _row("wire-3")["wire_text"]
    assert wire is not None and wire.startswith(CLAUDE_CODE_SYSTEM_PREFIX)
    assert "You are Aria." in wire


def test_wire_stranded_prompt_records_empty_string():
    """THE #2519 ALARM CLASS: a transform that clears the system message without
    moving it anywhere observable → wire_text '' (distinct from NULL), so the
    viewer can scream "nothing reached the wire" instead of showing the composed
    prompt as if it were delivered. This exact shape shipped for a full release."""

    class _StrandingTransform:
        def wrap_model_call(self, request, handler):
            return handler(request.override(system_message=None))

    req = _Req("gpt-5-codex", SystemMessage(content="STABLE"))
    with request_metadata_scope({"a2a.task_id": "wire-4"}):
        _run_wire_chain(req, _response(), _StrandingTransform())
    assert _row("wire-4")["wire_text"] == ""


# ── projected-context capture (ADR 0108 D2, #3191) ──────────────────────────


def test_projected_context_stash_is_captured():
    """KnowledgeMiddleware stashes projected context inside wrap_model_call;
    PromptCapture (outer) pops it and records it alongside the system prompt."""
    from graph.context_frame import stash_projected_context

    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={})

    def inner_handler(r):
        stash_projected_context("memory block", [{"label": "Hot memory", "chars": 12}])
        return _response()

    with request_metadata_scope({"a2a.task_id": "proj-1"}):
        capture.wrap_model_call(req, inner_handler)

    row = _row("proj-1")
    assert row["projected_context"] == "memory block"
    assert row["projected_sections"] == [{"label": "Hot memory", "chars": 12}]


def test_projected_context_accumulates_from_multiple_stashers():
    """Both KnowledgeMiddleware and ToolDeltaMiddleware may stash in the same
    call stack — stash_projected_context accumulates text and keeps the first
    sections."""
    from graph.context_frame import stash_projected_context

    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={})

    def inner_handler(r):
        stash_projected_context("knowledge ctx", [{"label": "Skills", "chars": 5}])
        stash_projected_context("tool delta notice")
        return _response()

    with request_metadata_scope({"a2a.task_id": "proj-2"}):
        capture.wrap_model_call(req, inner_handler)

    row = _row("proj-2")
    assert "knowledge ctx" in row["projected_context"]
    assert "tool delta notice" in row["projected_context"]
    assert row["projected_sections"] == [{"label": "Skills", "chars": 5}]


def test_projected_context_absent_when_nothing_stashed():
    """When no inner middleware stashes anything, projected columns are NULL."""
    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={})
    with request_metadata_scope({"a2a.task_id": "proj-3"}):
        capture.wrap_model_call(req, lambda _r: _response())

    row = _row("proj-3")
    assert row["projected_context"] is None
    assert row["projected_sections"] is None


def test_incognito_clears_projected_stash():
    """Incognito skips capture and clears the stash so it doesn't leak to
    a subsequent non-incognito call."""
    from graph.context_frame import pop_projected_context, stash_projected_context

    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="S"), state={"incognito": True})

    def inner_handler(r):
        stash_projected_context("leaked")
        return _response()

    with request_metadata_scope({"a2a.task_id": "proj-incog"}):
        capture.wrap_model_call(req, inner_handler)

    assert prompt_snapshots().calls_for_task("proj-incog") == []
    assert pop_projected_context() == (None, None)


def test_no_system_message_clears_projected_stash():
    """A request with no system message skips capture and clears the stash."""
    from graph.context_frame import pop_projected_context, stash_projected_context

    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", None, state={})

    def inner_handler(r):
        stash_projected_context("orphaned")
        return _response()

    with request_metadata_scope({"a2a.task_id": "proj-nosys"}):
        capture.wrap_model_call(req, inner_handler)

    assert prompt_snapshots().calls_for_task("proj-nosys") == []
    assert pop_projected_context() == (None, None)


def test_projected_context_crosses_a_child_task_boundary():
    """THE production shape (#3250): the inner middleware does not run in the outer's
    call stack — the handler is awaited and the inner runs in a CHILD asyncio task.

    A ContextVar ``set()`` in a child mutates that task's copy of the context and is
    invisible to the parent, so the original stash recorded nothing in production
    while every same-stack test above passed. On a live turn: 6267 characters
    stashed, popped as ``None``, and the column has no fallback since #3234 removed
    the legacy context channel. This test is the one that fails on that design.
    """
    from graph.context_frame import stash_projected_context

    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={})

    async def handler(_r):
        async def inner():  # the boundary the real pipeline puts between us
            stash_projected_context("memory block", [{"label": "Hot memory", "chars": 12}])
            return _response()

        return await asyncio.create_task(inner())

    async def _go():
        with request_metadata_scope({"a2a.task_id": "proj-task"}):
            await capture.awrap_model_call(req, handler)

    asyncio.run(_go())

    row = _row("proj-task")
    assert row["projected_context"] == "memory block"
    assert row["projected_sections"] == [{"label": "Hot memory", "chars": 12}]


def test_projected_context_does_not_leak_between_concurrent_calls():
    """Two turns in flight at once must not read each other's projection: each
    capture opens its OWN holder, and a child only ever inherits its own parent's."""
    from graph.context_frame import stash_projected_context

    async def one(task_id, text):
        capture = PromptCaptureMiddleware()
        req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={})

        async def handler(_r):
            async def inner():
                await asyncio.sleep(0)  # interleave the two turns
                stash_projected_context(text)
                return _response()

            return await asyncio.create_task(inner())

        with request_metadata_scope({"a2a.task_id": task_id}):
            await capture.awrap_model_call(req, handler)

    async def _go():
        await asyncio.gather(one("proj-conc-a", "context A"), one("proj-conc-b", "context B"))

    asyncio.run(_go())

    assert _row("proj-conc-a")["projected_context"] == "context A"
    assert _row("proj-conc-b")["projected_context"] == "context B"


def test_failed_model_call_leaves_no_frame_behind():
    """A raising handler must close its frame — otherwise the next call on this
    context pops a stale projection and files it against the wrong turn."""
    from graph.context_frame import pop_projected_context, stash_projected_context

    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={})

    def boom(_r):
        stash_projected_context("half-composed")
        raise RuntimeError("model call failed")

    with request_metadata_scope({"a2a.task_id": "proj-boom"}):
        with contextlib.suppress(RuntimeError):
            capture.wrap_model_call(req, boom)

    assert prompt_snapshots().calls_for_task("proj-boom") == []
    assert pop_projected_context() == (None, None)


def test_stash_outside_a_capture_frame_is_dropped():
    """No frame open (capture disabled, or a call that never reaches the middleware)
    ⇒ the stash goes nowhere rather than waiting to attach itself to a later call."""
    from graph.context_frame import pop_projected_context, stash_projected_context

    stash_projected_context("nobody is listening")
    assert pop_projected_context() == (None, None)


def test_context_sections_none_without_projected_stash():
    """ADR 0108 D2: the legacy state["context_sections"] fallback is removed.
    Without a projected-context stash, context_sections is always None."""
    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"))
    with request_metadata_scope({"a2a.task_id": "proj-legacy"}):
        _run_chained(req, _response(), capture=capture, cache=PromptCacheMiddleware(enabled=False))

    row = _row("proj-legacy")
    assert row["context_sections"] is None
    assert row["projected_context"] is None
