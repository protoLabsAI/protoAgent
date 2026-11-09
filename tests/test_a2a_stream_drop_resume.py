"""One checkpoint continuation for a dropped A2A model stream (#4959)."""

import httpx
import pytest

from graph.llm import StreamStallTimeout
from runtime import turn_activity
from tests import test_turn_driver_characterization_stream as characterization
from tests._turn_driver_fakes import FakeGoals, Raise, text
from tests.test_turn_driver_characterization_stream import (
    _OVERFLOW,
    _run,
    chat_mod,
    turn_control,
)


# Reuse the deterministic graph and external-boundary fixtures.
env = characterization.env
compaction = characterization.compaction

@pytest.mark.asyncio
@pytest.mark.parametrize("error", [StreamStallTimeout("between chunks"), httpx.ReadError("closed")])
async def test_drop_continues_same_thread_with_saved_work(env, monkeypatch, error):
    env.install()
    calls = []
    closed = []
    metadata = {"origin": "a2a", "a2a.task_id": "task-1", "model": "chosen"}
    carried = ["remaining_tool"]

    async def carry(config, fence):
        assert config["configurable"]["thread_id"] == "a2a:s1"
        return carried

    async def native(message, session_id, config, **kwargs):
        calls.append((message, session_id, config, kwargs))
        assert turn_control._thread_lock("a2a:s1").locked()
        assert turn_activity.holding_task(session_id) == "task-1"
        try:
            if len(calls) == 1:
                yield ("text", "partial")
                raise error
            yield ("text", "continued")
            yield ("done", "continued")
        finally:
            closed.append(len(calls))

    monkeypatch.setattr(chat_mod._turn_stream, "_carried_fence", carry)
    monkeypatch.setattr(chat_mod, "_run_native_turn", native)

    assert await _run(request_metadata=metadata, images=[("image/png", "data")]) == [
        ("text", "partial"),
        ("tool_start", chat_mod._STREAM_DROP_NOTICE),
        ("text", "continued"),
        ("done", "continued"),
    ]
    assert len(calls) == 2
    assert calls[1][:3] == (chat_mod._STREAM_DROP_RETRY_PROMPT, "s1", calls[0][2])
    assert calls[1][3] == {
        "request_metadata": metadata,
        "resume": False,
        "images": None,
        "overflow_retry": True,
        "fence": carried,
    }
    assert closed == [1, 2]
    assert turn_activity.holding_task("s1") is None
    assert not turn_control._thread_lock("a2a:s1").locked()
    assert env.graph.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second", "expected"),
    [
        (StreamStallTimeout("between chunks"), chat_mod._STREAM_STALL_MSG),
        (httpx.ReadError("closed"), chat_mod._STREAM_DROP_MSG),
        (ValueError("retry failed"), "retry failed"),
        (ValueError(_OVERFLOW), _OVERFLOW),
    ],
)
async def test_second_failure_is_terminal(env, compaction, second, expected):
    g = env.install(streams=[[Raise(StreamStallTimeout("first"))], [Raise(second)]])
    assert await _run() == [
        ("tool_start", chat_mod._STREAM_DROP_NOTICE),
        ("error", expected),
    ]
    assert len(g.stream_calls) == 2
    assert [v["messages"][0].content for _, v in g.updates] == [f"**Error:** {expected}"]
    assert compaction[0] == []


@pytest.mark.asyncio
async def test_other_errors_are_not_retried(env):
    g = env.install(streams=[[Raise(ValueError("bad input"))]])
    assert await _run() == [("error", "bad input")]
    assert len(g.stream_calls) == 1


@pytest.mark.asyncio
async def test_pre_turn_drop_is_not_retried(env, monkeypatch):
    g = env.install()

    async def pre_turn(*args):
        raise StreamStallTimeout("pre-turn failed")
        yield  # pragma: no cover — async generator seam

    monkeypatch.setattr(chat_mod._chat_dispatch, "_pre_turn_dispatch", pre_turn)
    assert await _run() == [("error", chat_mod._STREAM_STALL_MSG)]
    assert g.stream_calls == []


@pytest.mark.asyncio
async def test_overflow_retry_cannot_also_retry_a_drop(env, compaction):
    g = env.install(streams=[[Raise(ValueError(_OVERFLOW))], [Raise(httpx.ReadError("closed"))]])
    assert await _run() == [
        ("tool_start", chat_mod._OVERFLOW_NOTICE),
        ("error", chat_mod._STREAM_DROP_MSG),
    ]
    assert len(g.stream_calls) == 2
    assert len(compaction[0]) == 1


@pytest.mark.asyncio
async def test_goal_continuation_uses_recovery_prompt_without_kickoff(env, monkeypatch):
    goals = FakeGoals([("done", "met")])
    monkeypatch.setattr(env.state, "goal_controller", goals)
    g = env.install(streams=[[text("r1", "partial"), Raise(StreamStallTimeout("stall"))], [text("r2", "recovered")]])
    frames = await _run("original request")
    assert frames[-1] == ("done", "recovered\n\n---\nmet")
    assert g.stream_calls[1][0]["messages"][-1].content == chat_mod._STREAM_DROP_RETRY_PROMPT
    assert goals.kickoffs == ["original request"]
    assert g.updates == []
