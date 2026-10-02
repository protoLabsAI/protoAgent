"""A FOREGROUND ``delegate_to`` that times out on a still-working A2A peer hands the caller
the peer's task id, last state and status, and a way to collect the result later (#3775).

The live failure: designSystem sent ``delegate_to protoEngineer`` a boarding brief in the
foreground; at 600s the peer was still ``TASK_STATE_WORKING`` and the tool returned a bare
"still running" error — no task id, no progress, nothing to collect the card ids with
afterwards. #3700 fixed the BACKGROUND dispatch; these pin the same contract at the
foreground tool boundary, driven through the real ``delegate_to`` tool and a real
``DelegateRegistry`` over a fake peer that never finishes inside the call's window:

* the tool result names the task id, the last state and the peer's last status message,
  says how to collect the late result (``resume_task_id``), and never says to retry —
  on the room path (a chat session) and the plain path (no session) alike;
* the room path, which leaves a late collection running, says the answer is delivered
  automatically and does not also call the timeout a failure;
* collecting later actually works: ``delegate_to(..., resume_task_id=<id>)`` on a task that
  is STILL WORKING waits on that task with ``GetTask`` only (never a second ``SendMessage``,
  which would double-board the work) and returns the peer's answer once it lands — or, if
  it is still going, the same task-id-bearing message again instead of "can't be resumed";
* a lead that collects the answer itself withdraws the room's pending handle for that task,
  so the answer is not delivered a second time.
"""

from __future__ import annotations

import asyncio
import itertools
import json as _json
import time as _time

import httpx
import pytest

import runtime.state as rs
from plugins.delegates import _build_delegate_to, conversations, late
from plugins.delegates.registry import DelegateRegistry

PEER_URL = "https://peer.example/a2a"
SESSION = "sess-3775"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    """Fake ``httpx.AsyncClient``: records every posted body and answers via ``handler``.
    Has no ``get``, so the pre-flight card probe degrades gracefully."""

    def __init__(self, handler, bodies):
        self._handler = handler
        self._bodies = bodies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, headers=None):
        self._bodies.append(json or {})
        return self._handler(json or {})


def _task(state, *, status_text=None, answer=None, task_id="t9"):
    task = {"id": task_id, "contextId": "ctx-9", "status": {"state": state}}
    if status_text:
        task["status"]["message"] = {"parts": [{"text": status_text}]}
    if answer:
        task["artifacts"] = [{"parts": [{"text": answer}]}]
    return _Resp({"jsonrpc": "2.0", "result": {"task": task}})


def _working():
    return _task("TASK_STATE_WORKING", status_text="created 2 of 4 cards")


@pytest.fixture
def wire(monkeypatch):
    """Skip egress policy, make sleeps instant, advance the clock 0.3s per read, and return
    ``install(handler) -> bodies``."""
    monkeypatch.setattr("security.policy.check_url", lambda *_a, **_k: None)

    async def _noop(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    ticks = itertools.count(0.0, 0.3)
    monkeypatch.setattr(_time, "monotonic", lambda: next(ticks))
    bodies: list[dict] = []

    def _install(handler):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Client(handler, bodies))
        return bodies

    return _install


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(rs.STATE, "delegate_registry", None, raising=False)
    conversations.reset()
    late._RUNNING.clear()
    yield
    conversations.reset()
    late._RUNNING.clear()


@pytest.fixture
def collected(monkeypatch):
    """Record room late-collection starts instead of running the (hour-long) poll."""
    starts: list[tuple] = []

    def _collect_late(self, conversation_key, name, *, session_id="", incognito=False):
        starts.append((conversation_key, name, session_id))
        return conversations.pending_for(conversation_key, name, PEER_URL) is not None

    monkeypatch.setattr(DelegateRegistry, "collect_late", _collect_late)
    return starts


def _registry(**raw) -> DelegateRegistry:
    return DelegateRegistry([{"name": "peer", "type": "a2a", "url": PEER_URL, "poll_timeout_s": 1, **raw}])


async def _call(registry, **args):
    tool = _build_delegate_to(registry)
    result = await tool.ainvoke({"name": "delegate_to", "args": args, "id": "call-1", "type": "tool_call"})
    # The room path returns a Command whose FIRST message is the tool's ToolMessage.
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        return update["messages"][0].content
    return getattr(result, "content", result)


def _assert_collectable(out: str) -> None:
    assert "task t9" in out
    assert "state=TASK_STATE_WORKING" in out
    assert "created 2 of 4 cards" in out  # the peer's last status rides back
    # Exactly how to collect it: the target, a query and the task id.
    assert "delegate_to(target='peer'" in out
    assert "resume_task_id='t9'" in out
    assert "retry" not in out.lower()


# ── the timeout result ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_foreground_room_timeout_returns_task_id_status_and_auto_delivery(wire, collected):
    bodies = wire(lambda body: _working())

    out = await _call(
        _registry(), target="peer", query="board 4 cards", state={"session_id": SESSION, "messages": []}
    )

    _assert_collectable(out)
    assert "delivered automatically" in out
    # Not reported as a failure — the peer is fine, this side stopped waiting.
    assert "failed" not in out.lower()
    # …and the room really did leave a collection running for that task.
    assert collected and collected[0][1] == "peer"
    assert [b.get("method") for b in bodies].count("SendMessage") == 1


@pytest.mark.asyncio
async def test_foreground_plain_timeout_returns_task_id_status_and_collect_hint(wire):
    wire(lambda body: _working())

    # No chat session ⇒ the plain path: nothing collects on its own.
    out = await _call(_registry(), target="peer", query="board 4 cards", state={})

    _assert_collectable(out)
    assert "will NOT arrive on its own" in out


@pytest.mark.asyncio
async def test_foreground_explicit_timeout_also_returns_the_task_id(wire):
    # A peer that keeps progressing: only the explicit per-call cap stops the wait.
    n = itertools.count()
    wire(lambda body: _task("TASK_STATE_WORKING", status_text=f"created 2 of 4 cards (step {next(n)})"))

    out = await _call(_registry(poll_timeout_s=300), target="peer", query="board 4 cards", timeout=3, state={})

    assert "this call's timeout" in out
    assert "task t9" in out
    assert "resume_task_id='t9'" in out


# ── collecting later ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_task_id_on_a_working_task_collects_the_answer_without_resending(wire):
    replies = iter([_working(), _working(), _task("TASK_STATE_COMPLETED", answer="cards: C-1, C-2, C-3, C-4")])
    last = {}

    def handler(body):
        last["r"] = next(replies, None) or last["r"]
        return last["r"]

    bodies = wire(handler)

    out = await _call(_registry(), target="peer", query="collect", resume_task_id="t9", state={})

    assert out == "cards: C-1, C-2, C-3, C-4"
    methods = [b.get("method") for b in bodies]
    assert "SendMessage" not in methods  # a collection never re-sends the work
    assert all(b.get("params", {}).get("id") == "t9" for b in bodies)


@pytest.mark.asyncio
async def test_resume_task_id_on_a_task_still_working_returns_the_handle_again(wire):
    bodies = wire(lambda body: _working())

    out = await _call(_registry(), target="peer", query="collect", resume_task_id="t9", state={})

    _assert_collectable(out)
    assert "can't be resumed" not in out
    assert "SendMessage" not in [b.get("method") for b in bodies]


@pytest.mark.asyncio
async def test_collecting_it_yourself_withdraws_the_rooms_pending_handle(wire, collected):
    wire(lambda body: _working())
    reg = _registry()
    await _call(reg, target="peer", query="board 4 cards", state={"session_id": SESSION, "messages": []})
    assert conversations.snapshot_pending(), "the room timeout should leave a pending handle"

    wire(lambda body: _task("TASK_STATE_COMPLETED", answer="cards: C-1..C-4"))
    out = await _call(reg, target="peer", query="collect", resume_task_id="t9", state={})

    assert "cards: C-1..C-4" in out
    # The room's collection would otherwise deliver the same answer a second time.
    assert not conversations.snapshot_pending()
