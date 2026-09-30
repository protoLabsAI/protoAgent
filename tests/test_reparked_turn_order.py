"""A pause moved to a new task by a plain message stays the chat's LIVE turn (#3963).

Found in the 09-30 re-smoke of main 540a91c4. A session parked on ``ask_human``; a plain
message (no ``hitl_resume``) was held and re-parked the pause on a NEW task (#1560/#3930),
and the old task was completed with "Continued in task …" ~50 ms later. The durable turns
reader ordered by ``last_updated``, so the completion put the SUPERSEDED task after the one
still parked: the console took the completed bubble as the latest turn, never reattached,
and no form came back in any profile — a reply typed in the composer was then held and
re-asked on yet another task.

These drive the REAL pipeline — ``ProtoAgentExecutor`` under the a2a-sdk handler with the
parked-task router installed, into the durable SQLite task store — and read the result
back through the REAL turns route and session summary.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from a2a.types import AgentSkill, GetTaskRequest, Message, Part, Role, SendMessageRequest, TaskState
from fastapi import FastAPI

import protolabs_a2a as pa
from a2a_impl import hitl_routing
from a2a_impl.executor import ProtoAgentExecutor, set_progress_hook, set_terminal_hook
from a2a_impl.registry import harden_active_task_registry

CTX = "chat-3963"
CALL = ServerCallContext()
_HANDLERS: list = []


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    set_terminal_hook(None)
    set_progress_hook(None)
    monkeypatch.setattr("a2a_impl.registry.FLUSH_GRACE_S", 0.02)
    yield
    set_terminal_hook(None)
    set_progress_hook(None)
    hitl_routing._ROUTER[0] = None


@pytest.fixture(autouse=True)
async def _drain():
    yield
    for handler, router in _HANDLERS:
        await router.drain()
        tasks = set(getattr(handler._active_task_registry, "_cleanup_tasks", ()) or ())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
    _HANDLERS.clear()


async def _handler(tmp_path, calls: list):
    from a2a_impl.stores import ReasoningCoalescingTaskStore, make_sqlite_engine

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        calls.append({"text": text, "resume": resume})
        if resume:
            yield ("done", f"You like {text}.")
        else:
            yield ("input_required", {"question": "Favourite fruit?"})

    store = ReasoningCoalescingTaskStore(make_sqlite_engine(str(tmp_path / "a2a-tasks.db")))
    await store.initialize()
    card = pa.build_agent_card(
        name="t",
        description="d",
        url="http://t/a2a",
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])],
        bearer=False,
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream),
        task_store=store,
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )
    assert harden_active_task_registry(handler)
    router = hitl_routing.install_parked_task_routing(handler)
    assert router is not None
    _HANDLERS.append((handler, router))
    return handler, router, store.engine


def _msg(text: str, *, mid: str, hitl_resume: bool = False) -> SendMessageRequest:
    message = Message(message_id=mid, context_id=CTX, role=Role.ROLE_USER, parts=[Part(text=text)])
    if hitl_resume:
        message.metadata.update({"hitl_resume": True})
    return SendMessageRequest(message=message)


async def _read(monkeypatch, engine, path: str) -> dict:
    import operator_api.chat_routes as cr
    import runtime.state as rs

    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        return (await c.get(path)).json()


async def _park_then_repark(tmp_path):
    calls: list = []
    handler, router, engine = await _handler(tmp_path, calls)
    first = await handler.on_message_send(_msg("ask me my favourite fruit", mid="m1"), CALL)
    assert first.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    # A plain message — no hitl_resume — while parked: held, and re-parked on a NEW task.
    held = await handler.on_message_send(_msg("Also end your next reply with HELD.", mid="m2"), CALL)
    await router.drain()
    assert held.id != first.id
    settled = await handler.on_get_task(GetTaskRequest(id=first.id), CALL)
    # The shape that exposed the bug: the superseded task completed AFTER the new one parked.
    assert settled.status.state == TaskState.TASK_STATE_COMPLETED
    return handler, router, engine, first.id, held.id, calls


@pytest.mark.asyncio
async def test_turns_keep_a_reparked_pause_last_and_mark_it_live(monkeypatch, tmp_path):
    _handler_, _router, engine, first, held, _calls = await _park_then_repark(tmp_path)

    body = await _read(monkeypatch, engine, f"/api/chat/sessions/{CTX}/turns")
    turns = body["turns"]
    # Chronology is when each turn BEGAN: the question, then the held message that took
    # over its pause. Ordering by last_updated put the superseded task last (#3963).
    assert [t["task_id"] for t in turns] == [first, held]
    assert [t["state"] for t in turns] == ["TASK_STATE_COMPLETED", "TASK_STATE_INPUT_REQUIRED"]
    # The superseded completion was stamped later — the ordering must not depend on it.
    assert turns[0]["last_updated"] > turns[1]["last_updated"]
    # And the live marker names the parked task outright, whatever a reader's ordering.
    assert body["live_task_id"] == held


@pytest.mark.asyncio
async def test_session_summary_reports_the_reparked_pause(monkeypatch, tmp_path):
    """``last_state`` is the NEWEST turn's state — the parked one, not the completion that
    landed after it (the Zed shim reads this to tell whether the session waits on input)."""
    _handler_, _router, engine, _first, _held, _calls = await _park_then_repark(tmp_path)

    body = await _read(monkeypatch, engine, f"/api/chat/sessions/{CTX}")
    assert body["last_state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_the_superseded_task_names_its_successor_in_metadata(tmp_path):
    """The settle's status message says WHICH task took the pause over, as data — a console
    reattaching to the old task (a warm tab) follows it there instead of settling blind."""
    handler, _router, _engine, first, held, _calls = await _park_then_repark(tmp_path)

    settled = await handler.on_get_task(GetTaskRequest(id=first), CALL)
    metadata = dict(settled.status.message.metadata)
    assert metadata.get(hitl_routing.SUPERSEDED_BY) == held


@pytest.mark.asyncio
async def test_answering_the_reparked_pause_resumes_it_and_parks_nothing_new(monkeypatch, tmp_path):
    """No chain of re-parks: the form answer continues the task that holds the pause."""
    handler, router, engine, first, held, calls = await _park_then_repark(tmp_path)

    answer = await handler.on_message_send(_msg("kiwi", mid="m3", hitl_resume=True), CALL)
    await router.drain()
    assert answer.id == held
    assert answer.status.state == TaskState.TASK_STATE_COMPLETED
    assert calls[-1] == {"text": "kiwi", "resume": True}

    body = await _read(monkeypatch, engine, f"/api/chat/sessions/{CTX}/turns")
    assert [t["task_id"] for t in body["turns"]] == [first, held]
    assert body["live_task_id"] is None


@pytest.mark.asyncio
async def test_the_turns_tail_is_the_newest_created(monkeypatch, tmp_path):
    """The bounded tail keeps the most recently CREATED turns — the re-parked pause among
    them — even when an older task was updated last."""
    _handler_, _router, engine, _first, held, _calls = await _park_then_repark(tmp_path)

    body = await _read(monkeypatch, engine, f"/api/chat/sessions/{CTX}/turns?limit=1")
    assert [t["task_id"] for t in body["turns"]] == [held]
    assert body["live_task_id"] == held


@pytest.mark.asyncio
async def test_an_older_orphan_is_never_the_live_turn(monkeypatch, tmp_path):
    """Only the newest-created turn can be live. An older row left WORKING (a turn the
    process died under, not yet reaped) must not be named live over the newer turns — a
    reader would move it to the end of the chat."""
    from datetime import UTC, datetime

    from a2a.server.tasks.database_task_store import TaskModel

    from a2a_impl.stores import ReasoningCoalescingTaskStore, make_sqlite_engine

    store = ReasoningCoalescingTaskStore(make_sqlite_engine(str(tmp_path / "a2a-tasks.db")))
    await store.initialize()

    def row(task_id, state, minute):
        return {
            "id": task_id, "context_id": CTX, "kind": "task", "status": {"state": state},
            "artifacts": [], "history": [], "last_updated": datetime(2026, 9, 30, 12, minute, tzinfo=UTC),
        }

    async with store.engine.begin() as conn:
        await conn.execute(
            TaskModel.__table__.insert(),
            [row("orphan", "TASK_STATE_WORKING", 1), row("later", "TASK_STATE_COMPLETED", 2)],
        )

    body = await _read(monkeypatch, store.engine, f"/api/chat/sessions/{CTX}/turns")
    assert [t["task_id"] for t in body["turns"]] == ["orphan", "later"]
    assert body["live_task_id"] is None
