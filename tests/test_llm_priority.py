"""Priority tagging for the model in-flight limiter (ADR 0115 C5, #3760).

The limiter mechanism and its wiring are proven elsewhere (``tests/test_llm_limiter.py``,
``tests/test_llm_inflight.py``). This file proves the three TAGGING points and the plugin
seam feed the right class into the limiter's priority ContextVar:

- ``server/chat.py`` — operator chat / console turns run under ``interactive``; A2A,
  background and scheduled turns stay ``default`` (they are deliberately left untagged);
- ``plugins/workflows/engine.py`` — recipe fan-out steps run under ``bulk``, scoped to the
  step execution;
- ``graph.sdk.llm_priority`` — sets the class for its block (sync or async), rejects an
  unknown class, restores the previous class on exit, and is inherited by ``asyncio`` tasks;

plus an end-to-end proof, with a fake model boundary and a limit-1 lane, that a queued
``interactive`` call overtakes a ``bulk`` call that queued FIRST.
"""

from __future__ import annotations

import asyncio

import pytest

from graph import llm_limiter, sdk
from graph.llm import _guarded_reconnecting_stream
from graph.llm_limiter import BULK, DEFAULT, INTERACTIVE, get_priority


@pytest.fixture(autouse=True)
def _reset():
    llm_limiter._reset_for_tests()
    yield
    llm_limiter._reset_for_tests()


async def _until(predicate, *, tries: int = 5000) -> None:
    """Spin the event loop until ``predicate()`` holds — deterministic ordering without
    sleeps that could race under load."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


# ── sdk.llm_priority (r3 / AC3) ────────────────────────────────────────────────────────


def test_llm_priority_rejects_unknown_class():
    with pytest.raises(ValueError) as excinfo:
        sdk.llm_priority("urgent")
    msg = str(excinfo.value)
    assert "urgent" in msg
    # the message names the three valid classes
    for cls in (INTERACTIVE, DEFAULT, BULK):
        assert cls in msg
    # rejected at call time, BEFORE entering — the current class is untouched
    assert get_priority() == DEFAULT


def test_llm_priority_sync_sets_and_restores():
    assert get_priority() == DEFAULT
    with sdk.llm_priority("bulk") as active:
        assert active == BULK
        assert get_priority() == BULK
    assert get_priority() == DEFAULT  # restored on exit


async def test_llm_priority_async_sets_and_nests():
    assert get_priority() == DEFAULT
    async with sdk.llm_priority("bulk"):
        assert get_priority() == BULK
        async with sdk.llm_priority("interactive"):
            assert get_priority() == INTERACTIVE
        assert get_priority() == BULK  # inner scope restored the enclosing class
    assert get_priority() == DEFAULT


async def test_llm_priority_inherited_by_asyncio_task():
    seen: dict[str, str] = {}

    async def child():
        # a task created inside the block copies the current context → inherits the class
        seen["priority"] = get_priority()

    async with sdk.llm_priority("bulk"):
        await asyncio.create_task(child())
    assert seen["priority"] == BULK


# ── server/chat.py tagging (r1 / AC1) ────────────────────────────────────────────────────


def test_is_interactive_origin_classification():
    from server.chat import is_interactive_origin

    # operator chat / console turns (incl. the streaming A2A path's empty origin), case- and
    # whitespace-insensitive
    for origin in ("", "local", "api-chat", "console", "API-Chat", "  api-chat "):
        assert is_interactive_origin(origin) is True, origin
    # A2A, the server-fired autonomous origins, and the programmatic API surfaces stay default
    for origin in (
        "a2a",
        "scheduler",
        "watch",
        "inbox",
        "webhook",
        "background",
        "background-resume",
        "delegate-result",
        "v1",
        "plugin",
    ):
        assert is_interactive_origin(origin) is False, origin


def test_interactive_turn_priority_tags_operator_turns():
    from server.chat import _interactive_turn_priority

    for origin in ("api-chat", "", "local", "console"):
        with _interactive_turn_priority(origin):
            assert get_priority() == INTERACTIVE, origin
        assert get_priority() == DEFAULT  # restored on exit


def test_interactive_turn_priority_leaves_others_default():
    from server.chat import _interactive_turn_priority

    # A2A, background and scheduled turns are NOT tagged — they stay default
    for origin in ("a2a", "background", "scheduler", "background-resume", "v1", "plugin"):
        with _interactive_turn_priority(origin):
            assert get_priority() == DEFAULT, origin


# ── plugins/workflows/engine.py tagging (r2 / AC2) ──────────────────────────────────────


async def test_workflow_fanout_steps_run_under_bulk():
    from plugins.workflows import engine

    seen: dict[str, str] = {}

    async def fake_run_step(subagent: str, prompt: str, sid: str) -> str:
        # the class the limiter would read at slot acquisition for this step's subagent
        seen[sid] = get_priority()
        return f"out-{sid}"

    recipe = {
        "name": "panel",
        "steps": [
            {"id": "a", "subagent": "finder", "prompt": "find a"},
            {"id": "b", "subagent": "finder", "prompt": "find b"},
        ],
    }
    result = await engine.execute_workflow(recipe, {}, run_step=fake_run_step)
    assert result["failed"] == []
    assert seen == {"a": BULK, "b": BULK}
    # scoped to the step execution — the engine's own context is left untouched
    assert get_priority() == DEFAULT


# ── end to end: interactive overtakes bulk through the wired stream (r4 / AC4) ───────────


def _blocking_make(block: asyncio.Event | None):
    """A fake ``_astream`` factory (the model boundary). The holder blocks until ``block``
    is set, so it holds the one slot; a waiter passes ``None``, so once granted a slot its
    stream is exhausted at once."""

    def make():
        async def gen():
            if block is not None:
                await block.wait()
            return
            yield  # pragma: no cover — makes gen() an async generator

        return gen()

    return make


def _queued(lane: str, priority: str) -> int:
    for rec in llm_limiter.snapshot()["lanes"]:
        if rec["lane"] == lane:
            return rec["queued_by_priority"][priority]
    return 0


def _inflight(lane: str) -> int:
    for rec in llm_limiter.snapshot()["lanes"]:
        if rec["lane"] == lane:
            return rec["inflight"]
    return 0


async def test_interactive_call_overtakes_queued_bulk_end_to_end():
    # limit 1 → the reserve clamps to 0, so this proves the priority ORDERING, not the
    # reserve: a later interactive waiter still beats an earlier bulk waiter.
    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)
    lane = "gw|smart"
    holder_release = asyncio.Event()
    order: list[str] = []

    async def call(name: str, priority: str, block: asyncio.Event | None) -> None:
        with llm_limiter.priority_scope(priority):
            stream = _guarded_reconnecting_stream(
                _blocking_make(block), timeout=None, max_retries=0, label=name, lane=lane
            )
            async for _ in stream:
                pass
        order.append(name)

    holder = asyncio.create_task(call("holder", DEFAULT, holder_release))
    await _until(lambda: _inflight(lane) == 1)  # holder took the only slot

    # bulk queues FIRST, then interactive — so FIFO alone would serve bulk first
    bulk = asyncio.create_task(call("bulk", BULK, None))
    await _until(lambda: _queued(lane, "bulk") == 1)
    interactive = asyncio.create_task(call("interactive", INTERACTIVE, None))
    await _until(lambda: _queued(lane, "interactive") == 1)

    # release the holder — the freed slot goes to interactive despite bulk arriving first
    holder_release.set()
    await asyncio.wait_for(asyncio.gather(holder, bulk, interactive), timeout=5)

    assert order[0] == "holder"
    assert order.index("interactive") < order.index("bulk")
    assert order == ["holder", "interactive", "bulk"]
