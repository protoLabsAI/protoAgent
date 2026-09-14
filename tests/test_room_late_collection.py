"""Late collection — a room member the room stopped waiting on can still be heard (#3360b).

A member whose address trips its no-progress bound is dropped from the room's later rounds
and never re-addressed (a retry is a duplicate task — ``room_rounds._dropped``). Since #3360
the adapter gets the task id back at once, so the task can be COLLECTED instead: kept in
``conversations``' pending slot, polled read-only with ``GetTask`` (``late.collect``), and
delivered as the member's own late room message through the background manager.

What these pin, in the decision record's own test-matrix order:

1. a handle exists only after a non-terminal timeout — never on an answer, a park, an
   unreachable peer, a resume, or a dispatch with no conversation key;
2. while it is pending the member's contextId stays DROPPED;
3. collection sends ``GetTask`` and never ``SendMessage``;
4. a collected answer is delivered once, as the member's late room message, and the
   handle goes; a second start is a no-op;
5. FAILED is delivered as a failure and never teaches continuity;
6. a task the peer no longer knows is withdrawn with no error surface;
7. transport failures are retried, then given up on with a failure;
8. rewind / delete / fork / session-delete withdraw a collection, including mid-GetTask;
9. the room's round policy is unchanged — a collecting member is still never re-addressed;
10. at most one ``GetTask`` per poll.
"""

from __future__ import annotations

import asyncio
import importlib
import itertools
import json as _json
import time as _time
from unittest.mock import patch

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

import runtime.state as rs
from graph.config import LangGraphConfig
from graph.room_rounds import collecting_note
from plugins.delegates import conversations, late
from plugins.delegates.adapters import DelegateError
from plugins.delegates.registry import DelegateRegistry

sc = importlib.import_module("server.chat")

PEER_URL = "https://peer.example/a2a"
KEY = "thread-1"
SESSION = "sess-1"
_real_sleep = asyncio.sleep  # captured before any fixture swaps it for a no-op


# ── harness ───────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    """Fake ``httpx.AsyncClient``: records every posted body, answers via ``handler``
    (which may raise, to play a transport failure)."""

    def __init__(self, handler, bodies, **_kw):
        self._handler = handler
        self._bodies = bodies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, headers=None):
        self._bodies.append({"url": url, **(json or {})})
        return self._handler(url, json or {})


def _task(*, state, task_id="t9", context_id="ctx-9", text=None) -> dict:
    task = {"id": task_id, "status": {"state": state}}
    if context_id:
        task["contextId"] = context_id
    if text:
        task["artifacts"] = [{"parts": [{"text": text}]}]
    return task


def _ok(**kw) -> _Resp:
    return _Resp({"jsonrpc": "2.0", "result": {"task": _task(**kw)}})


def _parked(question="Which branch?") -> _Resp:
    task = _task(state="TASK_STATE_INPUT_REQUIRED")
    task["status"]["message"] = {"parts": [{"text": question}]}
    return _Resp({"jsonrpc": "2.0", "result": {"task": task}})


_NOT_FOUND = _Resp({"jsonrpc": "2.0", "error": {"code": -32001, "message": "Task not found"}})


def _methods(bodies) -> list[str]:
    return [b.get("method") for b in bodies]


@pytest.fixture
def wire(monkeypatch):
    """Install the fake transport; ``install(handler)`` returns the shared list of bodies."""
    monkeypatch.setattr("security.policy.check_url", lambda *_a, **_k: None)

    async def _noop(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    bodies: list[dict] = []

    def _install(handler):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(handler, bodies, **kw))
        return bodies

    return _install


@pytest.fixture(autouse=True)
def _clean():
    conversations.reset()
    late._RUNNING.clear()
    yield
    conversations.reset()
    late._RUNNING.clear()


class _Mgr:
    """Stands in for ``BackgroundManager.spawn_work``: runs the work at once and records
    how it settled, which is what the drain then renders."""

    def __init__(self):
        self.jobs: list[dict] = []

    async def spawn_work(self, **kw):
        try:
            kw["result"], kw["status"] = await kw["work"](), "completed"
        except Exception as exc:  # noqa: BLE001 — mirrors _run_work settling a failed job
            kw["result"], kw["status"] = str(exc), "failed"
        self.jobs.append(kw)
        return f"job-{len(self.jobs)}"


@pytest.fixture
def mgr(monkeypatch):
    m = _Mgr()
    monkeypatch.setattr(rs.STATE, "background_mgr", m, raising=False)
    return m


def _registry() -> DelegateRegistry:
    return DelegateRegistry([{"name": "peer", "type": "a2a", "url": PEER_URL}])


def _pending():
    return conversations.pending_for(KEY, "peer", PEER_URL)


async def _abandon(reg, install, monkeypatch, *, key=KEY, **dispatch_kw):
    """Address a peer that is still WORKING when the no-progress bound trips."""
    bodies = install(lambda _u, _b: _ok(state="TASK_STATE_WORKING"))
    ticks = itertools.count(0.0, 1000.0)
    with monkeypatch.context() as m:
        m.setattr(_time, "monotonic", lambda: next(ticks))
        with conversations.origin_session(SESSION):
            with pytest.raises(DelegateError, match="still running"):
                await reg.dispatch("peer", "do the long thing", conversation_key=key, **dispatch_kw)
    return bodies


async def _drain_collections():
    await asyncio.gather(*late.running().values())


# ── 1 + 2: the handle — only after a non-terminal timeout, continuity still dropped ──


async def test_giving_up_on_a_working_task_retains_it_and_still_drops_continuity(wire, monkeypatch):
    reg = _registry()
    wire(lambda _u, _b: _ok(state="TASK_STATE_COMPLETED", context_id="ctx-1", text="hi"))
    with conversations.origin_session(SESSION):
        await reg.dispatch("peer", "hello", conversation_key=KEY)
    assert conversations.remembered(KEY, "peer", PEER_URL) == "ctx-1"

    await _abandon(reg, wire, monkeypatch)

    assert _pending() == conversations._Pending("t9", "ctx-9", SESSION)
    # Retain the task, keep continuity dropped: the next address must open a fresh context
    # rather than queue behind the turn the room just gave up on.
    assert conversations.remembered(KEY, "peer", PEER_URL) == ""


async def test_the_next_address_while_pending_opens_a_fresh_context(wire, monkeypatch):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    bodies = wire(lambda _u, _b: _ok(state="TASK_STATE_COMPLETED", task_id="t10", context_id="ctx-10", text="ok"))

    await reg.dispatch("peer", "next question", conversation_key=KEY)

    send = next(b for b in bodies if b.get("method") == "SendMessage")
    assert "contextId" not in send["params"]["message"]


async def test_no_handle_when_the_address_answered(wire):
    wire(lambda _u, _b: _ok(state="TASK_STATE_COMPLETED", text="done"))
    await _registry().dispatch("peer", "q", conversation_key=KEY)
    assert conversations.snapshot_pending() == {}


async def test_no_handle_when_the_peer_parked(wire):
    wire(lambda _u, _b: _parked())
    reply = await _registry().dispatch("peer", "q", conversation_key=KEY)
    assert "needs input" in reply
    assert conversations.snapshot_pending() == {}


async def test_no_handle_when_the_peer_was_unreachable(wire):
    def _refuse(_u, _b):
        raise httpx.ConnectError("refused")

    wire(_refuse)
    with pytest.raises(DelegateError, match="unreachable"):
        await _registry().dispatch("peer", "q", conversation_key=KEY)
    assert conversations.snapshot_pending() == {}


async def test_no_handle_without_a_conversation_key(wire, monkeypatch):
    await _abandon(_registry(), wire, monkeypatch, key=None)
    assert conversations.snapshot_pending() == {}


async def test_no_handle_for_a_resume(wire, monkeypatch):
    """A resume answers the LEAD's parked task; it is not the room's to collect."""

    def _peer(_u, body):
        if body.get("method") == "GetTask" and body["params"]["id"] == "t0":
            return _parked()
        return _ok(state="TASK_STATE_WORKING", task_id="t0")

    wire(_peer)
    ticks = itertools.count(0.0, 1000.0)
    with monkeypatch.context() as m:
        m.setattr(_time, "monotonic", lambda: next(ticks))
        with pytest.raises(DelegateError, match="still running"):
            await _registry().dispatch("peer", "main", resume_task_id="t0")
    assert conversations.snapshot_pending() == {}


# ── 3 + 4 + 10: collection is GetTask-only and delivers the member's late message ──


async def test_collection_polls_with_get_task_only_and_delivers_the_late_answer(wire, monkeypatch, mgr):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    bodies = wire(lambda _u, _b: _ok(state="TASK_STATE_COMPLETED", text="the late answer"))
    start = len(bodies)

    assert reg.collect_late(KEY, "peer", session_id=SESSION) is True
    await _drain_collections()

    after = bodies[start:]
    assert _methods(after) == ["GetTask"]  # one poll, and no SendMessage — ever
    assert after[0]["params"] == {"id": "t9", "historyLength": 0}
    [job] = mgr.jobs
    assert job["result_author"] == "peer"
    assert job["origin_session"] == SESSION
    assert job["status"] == "completed"
    assert job["result"].startswith(late.LATE_MARKER)
    assert "the late answer" in job["result"]
    assert _pending() is None
    # The answer is now on this thread, so the peer's context may continue.
    assert conversations.remembered(KEY, "peer", PEER_URL) == "ctx-9"


async def test_a_working_task_is_polled_until_it_settles(wire, monkeypatch, mgr):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    script = iter([_ok(state="TASK_STATE_WORKING")] * 3 + [_ok(state="TASK_STATE_COMPLETED", text="finally")])
    bodies = wire(lambda _u, _b: next(script))
    start = len(bodies)

    reg.collect_late(KEY, "peer", session_id=SESSION)
    await _drain_collections()

    assert _methods(bodies[start:]) == ["GetTask"] * 4
    assert "finally" in mgr.jobs[0]["result"]


async def test_a_second_start_while_collecting_is_a_no_op(wire, monkeypatch):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    gate = asyncio.Event()
    calls = []

    async def _held_open(*a, **k):
        calls.append(a)
        await gate.wait()
        return late.WITHDRAWN, ""

    monkeypatch.setattr(late, "collect", _held_open)

    assert reg.collect_late(KEY, "peer", session_id=SESSION) is True
    assert reg.collect_late(KEY, "peer", session_id=SESSION) is True
    await _real_sleep(0)
    assert len(calls) == 1 and len(late.running()) == 1
    gate.set()
    await _drain_collections()


async def test_nothing_to_collect_is_false_and_starts_nothing():
    assert _registry().collect_late(KEY, "peer", session_id=SESSION) is False
    assert late.running() == {}


async def test_only_a2a_delegates_are_collected(wire, monkeypatch):
    reg = DelegateRegistry([{"name": "peer", "type": "openai", "url": PEER_URL, "model": "m"}])
    conversations.remember_pending(KEY, "peer", PEER_URL, "t9")
    assert reg.collect_late(KEY, "peer", session_id=SESSION) is False


# ── 5 + 6 + 7: failures, a forgotten task, transport trouble ──────────────────


async def test_a_failed_task_is_delivered_as_a_failure_and_never_learns(wire, monkeypatch, mgr):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    failed = _task(state="TASK_STATE_FAILED")
    failed["status"]["message"] = {"parts": [{"text": "disk full"}]}
    wire(lambda _u, _b: _Resp({"jsonrpc": "2.0", "result": {"task": failed}}))

    reg.collect_late(KEY, "peer", session_id=SESSION)
    await _drain_collections()

    [job] = mgr.jobs
    assert job["status"] == "failed"
    assert "unfinished task" in job["result"] and "disk full" in job["result"]
    assert conversations.remembered(KEY, "peer", PEER_URL) == ""
    assert _pending() is None


async def test_a_task_the_peer_no_longer_knows_is_withdrawn_quietly(wire, monkeypatch, mgr):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    wire(lambda _u, _b: _NOT_FOUND)

    reg.collect_late(KEY, "peer", session_id=SESSION)
    await _drain_collections()

    assert mgr.jobs == []
    assert _pending() is None


async def test_transport_failures_are_retried_and_the_answer_still_arrives(wire, monkeypatch, mgr):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    attempts = itertools.count()

    def _flaky(_u, _b):
        if next(attempts) < 3:
            raise httpx.ConnectError("peer restarting")
        return _ok(state="TASK_STATE_COMPLETED", text="made it")

    wire(_flaky)
    reg.collect_late(KEY, "peer", session_id=SESSION)
    await _drain_collections()

    assert "made it" in mgr.jobs[0]["result"]


async def test_too_many_transport_failures_give_up_with_a_failure(wire, monkeypatch, mgr):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    monkeypatch.setattr(late, "_MAX_TRANSPORT_FAILURES", 3)

    def _down(_u, _b):
        raise httpx.ConnectError("gone")

    bodies = wire(_down)
    start = len(bodies)
    reg.collect_late(KEY, "peer", session_id=SESSION)
    await _drain_collections()

    assert _methods(bodies[start:]) == ["GetTask"] * 3
    [job] = mgr.jobs
    assert job["status"] == "failed" and "lost touch" in job["result"]
    assert _pending() is None


async def test_a_task_that_parks_later_hands_the_question_and_resume_handle_to_the_lead(wire, monkeypatch, mgr):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    wire(lambda _u, _b: _parked("Which branch should I push to?"))

    reg.collect_late(KEY, "peer", session_id=SESSION)
    await _drain_collections()

    [job] = mgr.jobs
    assert job["status"] == "completed"
    assert "Which branch should I push to?" in job["result"]
    assert "resume_task_id='t9'" in job["result"]


async def test_a_newer_context_is_not_replaced_by_the_late_answers(wire, monkeypatch, mgr):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    conversations.remember(KEY, "peer", PEER_URL, "ctx-newer")
    wire(lambda _u, _b: _ok(state="TASK_STATE_COMPLETED", text="late"))

    reg.collect_late(KEY, "peer", session_id=SESSION)
    await _drain_collections()

    assert conversations.remembered(KEY, "peer", PEER_URL) == "ctx-newer"


async def test_a_collection_that_never_settles_stops_at_the_ceiling(wire, monkeypatch):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    wire(lambda _u, _b: _ok(state="TASK_STATE_WORKING"))
    ticks = itertools.count(0.0, 600.0)

    outcome, text = await late.collect(
        reg.get("peer"), KEY, "", _pending(), sleep=lambda _s: _real_sleep(0), clock=lambda: next(ticks)
    )

    assert outcome == late.FAILED and "stopped waiting" in text
    assert _pending() is None


async def test_no_background_manager_means_nothing_is_delivered(monkeypatch):
    monkeypatch.setattr(rs.STATE, "background_mgr", None, raising=False)
    assert await late.deliver("peer", "t9", SESSION, late.ANSWERED, "x") is False


# ── 8: erasing history withdraws a collection ─────────────────────────────────


@pytest.mark.parametrize(
    "erase",
    [
        lambda: conversations.forget(KEY),  # rewind / delete / fork destination
        lambda: conversations.forget_by_session(SESSION),  # session delete
    ],
    ids=["forget", "forget_by_session"],
)
async def test_erasing_the_conversation_withdraws_a_running_collection(wire, monkeypatch, erase):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)
    bodies = wire(lambda _u, _b: _ok(state="TASK_STATE_WORKING"))
    start = len(bodies)
    naps = itertools.count()

    async def _sleep(_s):
        if next(naps) == 1:
            erase()
        await _real_sleep(0)

    outcome, _ = await late.collect(reg.get("peer"), KEY, "", _pending(), sleep=_sleep)

    assert outcome == late.WITHDRAWN
    assert _methods(bodies[start:]) == ["GetTask"]  # nothing polled after the erase


async def test_an_erase_during_the_final_get_task_still_withdraws(wire, monkeypatch):
    reg = _registry()
    await _abandon(reg, wire, monkeypatch)

    def _answer_after_a_rewind(_u, _b):
        conversations.forget(KEY)
        return _ok(state="TASK_STATE_COMPLETED", text="too late")

    wire(_answer_after_a_rewind)
    outcome, _ = await late.collect(reg.get("peer"), KEY, "", _pending(), sleep=lambda _s: _real_sleep(0))

    assert outcome == late.WITHDRAWN
    assert conversations.remembered(KEY, "peer", PEER_URL) == ""


# ── the pending slot's own contract ───────────────────────────────────────────


def test_forget_one_drops_continuity_but_keeps_the_pending_task():
    conversations.remember(KEY, "peer", PEER_URL, "ctx-1")
    conversations.remember_pending(KEY, "peer", PEER_URL, "t9")
    assert conversations.forget_one(KEY, "peer", PEER_URL) is True
    assert _pending().task_id == "t9"


def test_forget_counts_and_drops_both_slots():
    conversations.remember(KEY, "peer", PEER_URL, "ctx-1")
    conversations.remember_pending(KEY, "peer", PEER_URL, "t9")
    conversations.remember_pending("other-thread", "peer", PEER_URL, "t1")
    assert conversations.forget(KEY) == 2
    assert _pending() is None
    assert conversations.pending_for("other-thread", "peer", PEER_URL).task_id == "t1"


def test_a_settling_collection_never_drops_a_newer_handle():
    conversations.remember_pending(KEY, "peer", PEER_URL, "t-new")
    assert conversations.forget_pending(KEY, "peer", PEER_URL, task_id="t-old") is False
    assert _pending().task_id == "t-new"


def test_nothing_is_pending_without_a_key_or_a_task():
    conversations.remember_pending("", "peer", PEER_URL, "t9")
    conversations.remember_pending(KEY, "peer", PEER_URL, "")
    assert conversations.snapshot_pending() == {}


def test_the_pending_slot_is_keyed_on_the_credential_too():
    conversations.remember_pending(KEY, "peer", PEER_URL, "t9", credential="bearer:old")
    assert conversations.pending_for(KEY, "peer", PEER_URL, "bearer:new") is None
    assert conversations.pending_for(KEY, "peer", PEER_URL, "bearer:old").task_id == "t9"


def test_the_pending_slot_is_bounded():
    for i in range(conversations._MAX_ENTRIES + 5):
        conversations.remember_pending(f"k{i}", "peer", PEER_URL, f"t{i}")
    assert len(conversations.snapshot_pending()) == conversations._MAX_ENTRIES
    assert conversations.pending_for("k0", "peer", PEER_URL) is None


# ── the room: a note, and the round policy unchanged (9) ──────────────────────


def test_collecting_note_names_each_collecting_member_once_in_order():
    outcomes = [
        {"author": "b", "ok": False, "collecting": True},
        {"author": "a", "ok": True},
        {"author": "c", "ok": False, "collecting": False},
        {"author": "b", "ok": False, "collecting": True},
    ]
    assert collecting_note(outcomes) == (
        "_Still waiting on @b in the background — a late answer will be posted here if it finishes._"
    )
    assert collecting_note([{"author": "a", "ok": True}]) == ""


class _LeadFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _Delegate:
    type = "a2a"
    url = PEER_URL


class _Room:
    """Two members: `fast` answers every round; `slow` gives up still working."""

    def __init__(self, *, collectable=True):
        self.calls: list[str] = []
        self.collect_calls: list[tuple] = []
        self._collectable = collectable

    def names(self):
        return ["fast", "slow"]

    def get(self, name):
        return _Delegate() if name in ("fast", "slow") else None

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
        self.calls.append(name)
        if name == "slow":
            raise DelegateError(
                "delegate 'slow' still running after 300s without observable progress — the peer may still be working"
            )
        return f"fast says {len(self.calls)}"

    def collect_late(self, conversation_key, name, *, session_id=""):
        self.collect_calls.append((conversation_key, name, session_id))
        return self._collectable


def _wire_room(monkeypatch, room, *, rounds=3):
    fake = _LeadFake(messages=iter([AIMessage(content="lead")]))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        graph = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
    cfg = LangGraphConfig()
    cfg.room_max_rounds = rounds
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "delegate_registry", room, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)


async def test_a_member_the_room_gave_up_on_is_collected_and_never_re_addressed(monkeypatch):
    room = _Room()
    _wire_room(monkeypatch, room)

    reply, outcomes = await sc._at_delegate_exchange("@fast @slow what broke?", "room-s1")

    # The round policy is untouched: `slow` failed once and was dropped — never retried —
    # and with one survivor the cast guard ended the room after round one.
    assert room.calls == ["fast", "slow"]
    assert room.collect_calls == [(sc._resolve_thread_id(None, "room-s1"), "slow", "room-s1")]
    slow = next(o for o in outcomes if o["author"] == "slow")
    assert slow["collecting"] is True and slow["ok"] is False
    assert "Still waiting on @slow" in reply


async def test_a_failure_with_nothing_to_collect_adds_no_note(monkeypatch):
    room = _Room(collectable=False)
    _wire_room(monkeypatch, room)

    reply, outcomes = await sc._at_delegate_exchange("@fast @slow what broke?", "room-s2")

    assert "Still waiting" not in reply
    assert next(o for o in outcomes if o["author"] == "slow")["collecting"] is False


async def test_no_session_means_no_collection(monkeypatch):
    room = _Room()
    _wire_room(monkeypatch, room)

    await sc._at_delegate_exchange("@fast @slow what broke?", "")

    assert room.collect_calls == []


# ── the lead's own delegate_to gets the same collection ───────────────────────


@pytest.mark.parametrize("collecting", [True, False])
async def test_delegate_to_tells_the_lead_a_late_answer_is_coming(monkeypatch, collecting):
    from langchain_core.messages import ToolMessage

    import graph.mention_op as mention_op
    from plugins.delegates import _dispatch_into_room

    room = _Room(collectable=collecting)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)

    async def _gave_up(*_a, **_k):
        return {"ok": False, "author": "slow", "reply": "", "error": "still running after 300s", "messages": []}

    monkeypatch.setattr(mention_op, "dispatch_into_room", _gave_up)

    command = await _dispatch_into_room(
        room, "slow", "run the migration", {"session_id": "lead-s1", "messages": []}, tool_call_id="tc-1"
    )

    [tool_message] = [m for m in command.update["messages"] if isinstance(m, ToolMessage)]
    assert room.collect_calls == [(sc._resolve_thread_id(None, "lead-s1"), "slow", "lead-s1")]
    assert ("delivered to you automatically" in tool_message.content) is collecting
    assert "Do not re-delegate" in tool_message.content if collecting else True
