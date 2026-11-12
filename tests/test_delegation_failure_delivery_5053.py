"""A failed background delegation is delivered promptly, exactly once — and across a
restart (#5053).

The incident: a sender delegated a brief to a peer in the background, then an
amendment. The brief's peer turn failed ("model provider closed the stream"), the
amendment finished first, and the sender only got the failure after it had already
diagnosed silent loss and resent everything.

These drive the real path — ``BackgroundManager.spawn_work`` running the A2A
adapter's ``dispatch`` against a fake peer at the httpx boundary, with real asyncio
timing — and pin what the sender sees:

* out-of-order completion: the amendment settles and drains first, the original's
  failure settles as soon as the peer reports it, each terminal hook fires once, and
  each completion drains exactly once and in completion order;
* failure before amendment: the failure is delivered before the amendment's answer;
* stream close: a peer that drops the SendMessage connection fails the job promptly
  with a transport cause, not a hang;
* delayed notification delivery: a job that turns terminal while a drain is in
  progress is delivered by the NEXT drain, never flipped to notified unseen;
* restart: a job settled (or interrupted) before a restart whose completion never
  reached its session is pushed to that session again at boot, once.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from background.manager import BackgroundManager
from background.store import INTERRUPTED_ERROR, BackgroundStore
from plugins.delegates.a2a import _DETACHED_DELEGATION
from plugins.delegates.adapters import A2aAdapter

URL = "http://peer.example/a2a"
STREAM_CLOSED = "model provider closed the stream (possibly rate-limited)"

_RealAsyncClient = httpx.AsyncClient


class FakePeer:
    """An A2A 1.0 peer that honours ``returnImmediately``: SendMessage returns a WORKING
    task at once and the task settles on the peer's own schedule (real asyncio time).
    ``plan`` maps a marker found in the message text to ``(delay_s, state, text)``."""

    def __init__(self, plan: dict[str, tuple[float, str, str]], *, drop_send_for: str = ""):
        self.plan = plan
        self.drop_send_for = drop_send_for
        self.tasks: dict[str, dict] = {}
        self.settled_at: dict[str, float] = {}
        self.received: list[dict] = []
        self._timers: list[asyncio.Task] = []

    def _task(self, tid: str) -> dict:
        return {"jsonrpc": "2.0", "result": {"task": self.tasks[tid]}}

    async def _settle(self, tid: str, delay: float, state: str, text: str) -> None:
        await asyncio.sleep(delay)
        status: dict = {"state": state}
        task = self.tasks[tid]
        if state == "TASK_STATE_COMPLETED":
            task["artifacts"] = [{"parts": [{"text": text}]}]
        else:
            status["message"] = {"parts": [{"text": text}]}
        task["status"] = status
        self.settled_at[tid] = time.monotonic()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(404)  # no agent card: the pre-flight probe degrades
        body = json.loads(request.content)
        method = body.get("method")
        if method == "SendMessage":
            msg = body["params"]["message"]
            self.received.append(body["params"])
            text = msg["parts"][0]["text"]
            if self.drop_send_for and self.drop_send_for in text:
                raise httpx.RemoteProtocolError("peer closed the connection without a response")
            marker = next(m for m in self.plan if m in text)
            delay, state, reply = self.plan[marker]
            tid = f"t-{marker}"
            self.tasks[tid] = {"id": tid, "contextId": f"c-{marker}", "status": {"state": "TASK_STATE_WORKING"}}
            self._timers.append(asyncio.create_task(self._settle(tid, delay, state, reply)))
            return httpx.Response(200, json=self._task(tid))
        if method == "GetTask":
            return httpx.Response(200, json=self._task(body["params"]["id"]))
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": -32601, "message": "nope"}})


@pytest.fixture
def peer_wire(monkeypatch):
    monkeypatch.setattr("security.policy.check_url", lambda *_a, **_k: None)

    def install(peer: FakePeer) -> None:
        transport = httpx.MockTransport(peer.handle)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=transport, **kw))

    return install


def _manager(tmp_path: Path, terminals: list) -> BackgroundManager:
    return BackgroundManager(
        agent_name="projectManager",
        invoke_url="http://127.0.0.1:0",
        store=BackgroundStore(str(tmp_path / "background" / "jobs.db")),
        on_terminal=lambda job: terminals.append((job.id, job.status, time.monotonic())),
    )


async def _delegate(mgr: BackgroundManager, session: str, query: str) -> str:
    adapter = A2aAdapter()
    d = adapter.parse({"name": "designSystem", "type": "a2a", "url": URL, "poll_timeout_s": 30})

    async def work() -> str:
        # What ``_spawn_background_delegation`` does at the top of its job coroutine.
        _DETACHED_DELEGATION.set(True)
        return await adapter.dispatch(d, query)

    return await mgr.spawn_work(
        origin_session=session, kind="delegate", description=f"delegate → designSystem: {query[:30]}",
        detail=query, work=work, result_author="designSystem",
    )


async def _settled(store: BackgroundStore, job_id: str, timeout: float = 10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = store.get(job_id)
        if job is not None and job.status != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"{job_id} never settled")


# ── ordering and latency on the live path ────────────────────────────────────────


async def test_failed_original_after_completed_amendment_delivers_promptly_and_once(tmp_path, peer_wire):
    peer = FakePeer({
        "ORIGINAL": (1.6, "TASK_STATE_FAILED", STREAM_CLOSED),
        "AMENDMENT": (0.0, "TASK_STATE_COMPLETED", "folded the amendment in"),
    })
    peer_wire(peer)
    terminals: list = []
    mgr = _manager(tmp_path, terminals)

    original = await _delegate(mgr, "s-pm", "ORIGINAL brief: design the Work view")
    amendment = await _delegate(mgr, "s-pm", "AMENDMENT: fold this in if you've started")

    amended = await _settled(mgr.store, amendment)
    assert amended.status == "completed"
    # The amendment's turn drains it alone; the original is still in flight at the peer.
    assert [j.id for j in mgr.store.drain_pending("s-pm")] == [amendment]

    failed = await _settled(mgr.store, original)
    assert failed.status == "failed"
    assert STREAM_CLOSED in failed.result
    # Prompt: settled within one poll interval of the peer reporting the failure.
    lag = [t for jid, _s, t in terminals if jid == original][0] - peer.settled_at["t-ORIGINAL"]
    assert lag < 2.0, f"failure reached the sender {lag:.2f}s after the peer reported it"
    # Exactly once: one terminal hook per job, completion order preserved.
    assert [(jid, s) for jid, s, _t in terminals] == [(amendment, "completed"), (original, "failed")]
    assert [j.id for j in mgr.store.drain_pending("s-pm")] == [original]
    assert mgr.store.drain_pending("s-pm") == []


async def test_failure_before_amendment_is_delivered_first(tmp_path, peer_wire):
    peer = FakePeer({
        "ORIGINAL": (0.0, "TASK_STATE_FAILED", STREAM_CLOSED),
        "AMENDMENT": (1.5, "TASK_STATE_COMPLETED", "done"),
    })
    peer_wire(peer)
    terminals: list = []
    mgr = _manager(tmp_path, terminals)

    original = await _delegate(mgr, "s-pm", "ORIGINAL brief")
    amendment = await _delegate(mgr, "s-pm", "AMENDMENT to the brief")
    await _settled(mgr.store, original)
    assert [j.id for j in mgr.store.drain_pending("s-pm")] == [original]
    await _settled(mgr.store, amendment)
    assert [(jid, s) for jid, s, _t in terminals] == [(original, "failed"), (amendment, "completed")]
    assert [j.id for j in mgr.store.drain_pending("s-pm")] == [amendment]


async def test_peer_closing_the_send_stream_fails_the_job_promptly(tmp_path, peer_wire):
    peer = FakePeer({"ORIGINAL": (0.0, "TASK_STATE_COMPLETED", "unused")}, drop_send_for="ORIGINAL")
    peer_wire(peer)
    terminals: list = []
    mgr = _manager(tmp_path, terminals)

    started = time.monotonic()
    original = await _delegate(mgr, "s-pm", "ORIGINAL brief")
    job = await _settled(mgr.store, original, timeout=5.0)
    assert job.status == "failed"
    assert "transport error" in job.result
    assert time.monotonic() - started < 3.0
    assert [(jid, s) for jid, s, _t in terminals] == [(original, "failed")]


# ── exactly-once drain under a concurrent settle ─────────────────────────────────


def test_a_job_settling_mid_drain_is_delivered_by_the_next_drain(tmp_path, monkeypatch):
    """The drain read the pending rows, then flipped EVERY pending row of the session —
    including one another writer settled in between, which was then never delivered."""
    store = BackgroundStore(str(tmp_path / "jobs.db"))
    first = store.create(agent_name="a", origin_session="s", subagent_type="delegate", description="one", prompt="")
    late = store.create(agent_name="a", origin_session="s", subagent_type="delegate", description="two", prompt="")
    store.mark_complete(first, "completed", "answer one")

    real_connect = store._connect

    class _Racing:
        """Wraps the drain's connection: right after its SELECT, a second connection
        settles ``late`` — the window a concurrently finishing job lands in."""

        def __init__(self, db: sqlite3.Connection):
            self._db = db
            self._fired = False

        def execute(self, sql, *args):
            cur = self._db.execute(sql, *args)
            if not self._fired and sql.lstrip().upper().startswith("SELECT"):
                self._fired = True
                other = sqlite3.connect(store.path, timeout=0.2)
                try:
                    other.execute(
                        "UPDATE background_jobs SET status='failed', result=?, completed_at=? WHERE id=?",
                        (STREAM_CLOSED, "2099-01-01T00:00:00+00:00", late),
                    )
                    other.commit()
                except sqlite3.OperationalError:
                    pass  # the drain holds the write lock: the settle lands after it
                finally:
                    other.close()
            return cur

        def __getattr__(self, name):
            return getattr(self._db, name)

    monkeypatch.setattr(store, "_connect", lambda: _Racing(real_connect()))
    drained = [j.id for j in store.drain_pending("s")]
    monkeypatch.setattr(store, "_connect", real_connect)
    if late not in drained:
        store.mark_complete(late, "failed", STREAM_CLOSED)  # no-op when the race write landed
        assert [j.id for j in store.drain_pending("s")] == [late], "a mid-drain settle was lost"
    assert first in drained
    assert store.drain_pending("s") == []


# ── across a restart ─────────────────────────────────────────────────────────────


def _restart(server_stores, db, monkeypatch):
    """One boot: build the manager the way startup does (reconcile first), install it,
    and re-arm the once-per-process latch."""
    import runtime.state as rs

    monkeypatch.setattr(server_stores, "_agent_store_db", lambda _name: db)
    monkeypatch.setattr(server_stores, "_BACKGROUND_REDELIVERY_DONE", False)
    mgr = server_stores._build_background_manager(None)
    assert mgr is not None
    monkeypatch.setattr(rs.STATE, "background_mgr", mgr, raising=False)
    return mgr


@pytest.fixture
def no_wakes(monkeypatch):
    """Any Activity wake fired from redelivery is a failure: a wake never drains."""
    from server import a2a as server_a2a

    wakes: list[str] = []
    monkeypatch.setattr(server_a2a, "_spawn_background_wake", lambda j: wakes.append(j.id))
    monkeypatch.setattr(server_a2a, "_spawn_background_resume", lambda _m, j: wakes.append(f"resume:{j.id}"))
    return wakes


async def test_restart_pushes_undelivered_terminals_once_per_session(tmp_path, monkeypatch, no_wakes):
    """A job interrupted by a restart (or one that settled just before it, its push-resume
    lost with the process) reaches its session at boot — one turn per session, whose drain
    hands every pending outcome over — not after an operator happens to type there."""
    from server import stores as server_stores

    db = tmp_path / "background" / "jobs.db"
    store = BackgroundStore(str(db))

    def job(desc: str, session: str = "s-pm", **kw) -> str:
        return store.create(agent_name="a", origin_session=session, subagent_type="delegate", description=desc,
                            prompt="", **kw)

    delivered = job("seen before the restart")
    store.mark_complete(delivered, "completed", "seen")
    assert [j.id for j in store.drain_pending("s-pm")] == [delivered]
    stale = job("settled long ago")
    store.mark_complete(stale, "failed", "old", now=datetime.now(UTC) - timedelta(days=3))
    canceled = job("operator stopped it")
    store.mark_complete(canceled, "canceled", "Canceled.")
    settled = job("settled, push-resume lost with the process")
    store.mark_complete(settled, "failed", STREAM_CLOSED)
    interrupted = job("in flight at the restart")
    other = job("another session's outcome", session="s-design")
    store.mark_complete(other, "completed", "done")
    # Outcomes no push-resume can drain: their Activity wake fired at settle; a replay is
    # a fresh LLM turn that delivers nothing.
    orphan = job("no origin session", session="")
    store.mark_complete(orphan, "failed", STREAM_CLOSED)
    hidden = job("incognito origin", origin_incognito=True)
    store.mark_complete(hidden, "failed", STREAM_CLOSED)

    pushes: list[tuple[str, list[str]]] = []

    async def fake_resume(self, session, jobs):
        pushes.append((session, [j.id for j in jobs]))
        self.store.drain_pending(session)  # the pushed turn's drain
        return True

    monkeypatch.setattr(BackgroundManager, "resume_origin_session", fake_resume)

    mgr = _restart(server_stores, db, monkeypatch)
    assert mgr.store.get(interrupted).status == "failed"
    assert mgr.store.get(interrupted).error == INTERRUPTED_ERROR
    assert pushes == [], "the boot build itself delivers nothing; the startup pass does"

    assert await server_stores.redeliver_undelivered_background() == 2
    assert sorted(pushes) == [("s-design", [other]), ("s-pm", [settled, interrupted])]
    assert no_wakes == []
    # Once per process.
    assert await server_stores.redeliver_undelivered_background() == 0

    # A second restart: everything pushable drained, the rest is never replayed.
    pushes.clear()
    _restart(server_stores, db, monkeypatch)
    assert await server_stores.redeliver_undelivered_background() == 0
    assert pushes == [] and no_wakes == []


async def test_a_failed_redelivery_push_retries_on_the_next_boot_without_a_wake(tmp_path, monkeypatch, no_wakes):
    from server import stores as server_stores

    db = tmp_path / "background" / "jobs.db"
    store = BackgroundStore(str(db))
    first = store.create(agent_name="a", origin_session="s-pm", subagent_type="delegate", description="one",
                         prompt="")
    second = store.create(agent_name="a", origin_session="s-pm", subagent_type="delegate", description="two",
                          prompt="")
    attempts: list[list[str]] = []

    async def failing_resume(self, session, jobs):
        attempts.append([j.id for j in jobs])
        return False  # the self-POST did not land: nothing drained

    monkeypatch.setattr(BackgroundManager, "resume_origin_session", failing_resume)
    _restart(server_stores, db, monkeypatch)  # both reconciled as interrupted
    assert await server_stores.redeliver_undelivered_background() == 1
    _restart(server_stores, db, monkeypatch)
    assert await server_stores.redeliver_undelivered_background() == 1
    assert attempts == [[first, second], [first, second]]
    assert no_wakes == [], "a failed push never falls back to a wake that cannot drain"
    # Still pending: the session's next turn drains both, once.
    assert [j.id for j in store.drain_pending("s-pm")] == [first, second]


async def test_resume_origin_session_sends_one_nudge_for_many_jobs(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, [])
    a = mgr.store.create(agent_name="a", origin_session="s", subagent_type="delegate", description="a", prompt="",
                         origin_fence=["read_file", "web_search"])
    b = mgr.store.create(agent_name="a", origin_session="s", subagent_type="delegate", description="b", prompt="",
                         origin_fence=["read_file"])
    sent: list[dict] = []

    async def fake_send(*, context_id, text, metadata):
        sent.append({"context_id": context_id, "text": text, "metadata": metadata})
        return "task-1"

    monkeypatch.setattr(mgr, "_send_a2a_message", fake_send)
    jobs = [mgr.store.get(a), mgr.store.get(b)]
    assert await mgr.resume_origin_session("s", jobs) is True
    assert len(sent) == 1
    assert sent[0]["context_id"] == "s"
    assert a in sent[0]["text"] and b in sent[0]["text"]
    assert sent[0]["metadata"]["origin"] == "background-resume"
    assert sent[0]["metadata"]["subagent_fence"] == ["read_file"]  # narrowest wins


# ── identity, delivery state, amendment linkage ─────────────────────────────────


async def test_a_peer_accepted_request_records_its_identity_and_delivery_state(tmp_path, peer_wire):
    """A failed delegation the peer had accepted is a KNOWN failed request: the row names
    the peer's task and our message id, and says whether the outcome has been delivered."""
    peer = FakePeer({"ORIGINAL": (0.0, "TASK_STATE_FAILED", STREAM_CLOSED)})
    peer_wire(peer)
    mgr = _manager(tmp_path, [])

    original = await _delegate(mgr, "s-pm", "ORIGINAL brief")
    job = await _settled(mgr.store, original)
    assert job.peer_task_id == "t-ORIGINAL"
    assert job.peer_message_id == peer.received[0]["message"]["messageId"]
    assert job.to_dict()["delivery"] == "undelivered"
    mgr.store.drain_pending("s-pm")
    assert mgr.store.get(original).to_dict()["delivery"] == "delivered"


async def test_a_never_accepted_request_has_no_peer_identity(tmp_path, peer_wire):
    peer = FakePeer({"ORIGINAL": (0.0, "TASK_STATE_COMPLETED", "unused")}, drop_send_for="ORIGINAL")
    peer_wire(peer)
    mgr = _manager(tmp_path, [])
    job = await _settled(mgr.store, await _delegate(mgr, "s-pm", "ORIGINAL brief"))
    assert job.status == "failed" and job.peer_task_id == "" and job.peer_message_id == ""


async def test_failed_delegate_drain_note_names_the_peer_task(tmp_path, peer_wire, monkeypatch):
    import importlib

    import runtime.state as rs

    chat = importlib.import_module("server.chat")
    peer = FakePeer({"ORIGINAL": (0.0, "TASK_STATE_FAILED", STREAM_CLOSED)})
    peer_wire(peer)
    mgr = _manager(tmp_path, [])
    monkeypatch.setattr(rs.STATE, "background_mgr", mgr, raising=False)
    await _settled(mgr.store, await _delegate(mgr, "s-pm", "ORIGINAL brief"))
    _msgs, replies = chat._drain_background("s-pm")
    assert len(replies) == 1 and replies[0]["ok"] is False
    assert "designSystem task t-ORIGINAL" in replies[0]["text"]
    assert STREAM_CLOSED in replies[0]["text"]


async def test_amendment_names_the_request_it_amends_to_the_peer(tmp_path, peer_wire, monkeypatch):
    """The amendment reaches the peer carrying the original's peer task id, our message id
    and its state on this side — and the link is persisted on the amendment's row."""
    import runtime.state as rs
    from plugins.delegates import _build_delegate_to
    from plugins.delegates.registry import DelegateRegistry

    peer = FakePeer({
        "ORIGINAL": (0.0, "TASK_STATE_FAILED", STREAM_CLOSED),
        "AMENDMENT": (0.0, "TASK_STATE_COMPLETED", "folded in"),
    })
    peer_wire(peer)
    mgr = _manager(tmp_path, [])
    monkeypatch.setattr(rs.STATE, "background_mgr", mgr, raising=False)
    registry = DelegateRegistry([
        {"name": "designSystem", "type": "a2a", "url": URL, "poll_timeout_s": 30},
        {"name": "other", "type": "a2a", "url": URL},
    ])
    delegate_to = _build_delegate_to(registry)
    state = {"session_id": "s-pm"}

    out = await delegate_to.coroutine(target="designSystem", query="ORIGINAL brief: design the Work view.",
                                      background=True, state=state)
    original = out.split("job `", 1)[1].split("`", 1)[0]
    await _settled(mgr.store, original)

    refused = await delegate_to.coroutine(target="other", query="AMENDMENT x", background=True, amends=original,
                                          state=state)
    assert refused.startswith("Error:")
    unknown = await delegate_to.coroutine(target="designSystem", query="AMENDMENT x", background=True,
                                          amends="bg-000000000000", state=state)
    assert unknown.startswith("Error:")

    out = await delegate_to.coroutine(target="designSystem", query="AMENDMENT: fold this in if you've started.",
                                      background=True, amends=original, state=state)
    amendment = out.split("job `", 1)[1].split("`", 1)[0]
    await _settled(mgr.store, amendment)

    sent = peer.received[-1]["message"]["parts"][0]["text"]
    original_row = mgr.store.get(original)
    assert f"job {original}" in sent
    assert "your task t-ORIGINAL" in sent
    assert f"message {original_row.peer_message_id}" in sent
    assert "ended failed before you answered it" in sent and STREAM_CLOSED in sent
    assert "ORIGINAL brief: design the Work view." in sent
    assert sent.rstrip().endswith("AMENDMENT: fold this in if you've started.")
    assert mgr.store.get(amendment).amends == original
