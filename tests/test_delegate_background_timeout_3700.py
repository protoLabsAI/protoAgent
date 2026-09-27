"""Background A2A delegate: judge progress by the PEER's task state, and never lose a
finished peer's reply to the poll deadline (#3700).

The live failure: a background ``delegate_to`` to a PM peer sat WORKING past the 300s
``poll_timeout_s`` and came back FAILED ("still running … without observable progress")
even though the peer had finished — the reply was lost, and the caller had no task id, so
the only recovery was a re-send that double-boards the work.

What these pin:

* **r1** — a peer whose STATUS MESSAGE keeps changing (but emits no answer text) is not
  timed out while those changes keep arriving: a status-message change is progress and
  resets the deadline (``_a2a_progress_fingerprint`` already carries the status message).
* **r2** — when the deadline genuinely expires the error names the peer task id, its last
  state and status message, and how to resume/collect it — and never says to retry. Both
  the no-progress bound and an explicit per-call timeout.
* **r3** — a BACKGROUND (detached) delegation whose peer finishes AFTER the deadline keeps
  polling the task (``late.collect_task``) and delivers the peer's real reply as the job
  result; a park is delivered too; a peer that never finishes still fails with the
  actionable, task-id-bearing message (not a bare failure, not "retry"); and a peer that
  fails while we wait surfaces its own diagnostic.
"""

from __future__ import annotations

import asyncio
import json as _json
import time as _time

import httpx
import pytest

from plugins.delegates import late
from plugins.delegates.adapters import (
    _DETACHED_DELEGATION,
    A2aAdapter,
    DelegateError,
    _status_message_text,
)

A = A2aAdapter()

# Non-loopback so ``_a2a_headers`` never reaches for a fleet service token (that would
# touch the real instance root); ``check_url`` is stubbed off in ``patched``.
URL = "http://peer.example/a2a"


def _parse(**raw):
    return A.parse({"name": "peer", "type": "a2a", "url": URL, **raw})


# ── wire harness ────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class _PeerClient:
    """Fake ``httpx.AsyncClient`` backed by shared ``state``: ``SendMessage`` returns a
    fixed reply; ``GetTask`` calls ``state['gets'](n)`` with a monotonically rising call
    index (used by BOTH the dispatch poll loop and the late extension). Has no ``get``, so
    the pre-flight card probe fails gracefully — exactly as it does in the a2a robustness
    suite."""

    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, headers=None):
        method = (json or {}).get("method")
        self.state["methods"].append(method)
        if method == "SendMessage":
            return self.state["send"]
        n = self.state["gets_seen"]
        self.state["gets_seen"] += 1
        return self.state["gets"](n)


def _install_peer(monkeypatch, *, send, gets):
    """Install the fake transport. ``gets`` is a callable ``(n) -> _Resp`` for the n-th
    GetTask. Returns the shared state dict (``methods``, ``gets_seen``)."""
    state = {"send": send, "gets": gets, "methods": [], "gets_seen": 0}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _PeerClient(state))
    return state


def _script(items):
    """A ``gets`` callable that walks ``items`` and then repeats the last one forever."""
    return lambda n: items[min(n, len(items) - 1)]


@pytest.fixture
def patched(monkeypatch):
    """Allow the url (skip egress policy) and make every sleep instant."""
    monkeypatch.setattr("security.policy.check_url", lambda *_a, **_k: None)

    async def _noop(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    return monkeypatch


def _clock(monkeypatch, step=1.0):
    """A fake ``time.monotonic`` that ADVANCES by ``step`` on every read (never freezes, so
    a miscounted read fails an assertion instead of spinning forever). Returns the list of
    values handed out."""
    reads: list[float] = []

    def _monotonic() -> float:
        reads.append(step * len(reads))
        return reads[-1]

    monkeypatch.setattr(_time, "monotonic", _monotonic)
    return reads


def _working(*, task_id="t1", msg=None):
    task = {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}
    if msg is not None:
        task["status"]["message"] = {"parts": [{"text": msg}]}
    return _Resp({"jsonrpc": "2.0", "result": {"task": task}})


def _completed(text, *, task_id="t1"):
    task = {"id": task_id, "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [{"text": text}]}]}
    return _Resp({"jsonrpc": "2.0", "result": {"task": task}})


def _parked(question, *, task_id="t1"):
    task = {"id": task_id, "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": [{"text": question}]}}}
    return _Resp({"jsonrpc": "2.0", "result": {"task": task}})


def _failed(msg, *, task_id="t1"):
    task = {"id": task_id, "status": {"state": "TASK_STATE_FAILED", "message": {"parts": [{"text": msg}]}}}
    return _Resp({"jsonrpc": "2.0", "result": {"task": task}})


async def _detached(coro_factory):
    """Run a dispatch inside a DETACHED (background) context — the flag
    ``_spawn_background_delegation`` sets via ``mark_delegation_detached``. Set inside the
    ``asyncio.run`` task, so it never leaks to the outer context."""
    _DETACHED_DELEGATION.set(True)
    return await coro_factory()


# ── r1: a changing status message is progress, even with no answer text ────────


def test_status_message_change_keeps_a_silent_peer_alive_past_the_poll_timeout(patched):
    """A PM peer running a long tool chain emits no answer text — only status-message
    updates. Each change resets the no-progress deadline, so the peer is NOT timed out while
    they keep arriving, and its eventual answer comes back (r1)."""
    reads = _clock(patched, step=1.0)
    state = _install_peer(
        patched,
        send=_working(msg="starting"),
        gets=_script(
            [
                _working(msg="reading the codebase"),
                _working(msg="running the audit"),
                _working(msg="writing the report"),
                _completed("the audit is done"),
            ]
        ),
    )

    # poll_timeout_s=3 with a clock that ticks 1s/read: without the reset the deadline trips
    # almost at once; with it the peer runs well past 3s and still answers.
    assert asyncio.run(A.dispatch(_parse(poll_timeout_s=3), "audit the design system")) == "the audit is done"
    assert reads[-1] - reads[0] > 3
    assert state["methods"].count("GetTask") >= 3


# ── r2: the deadline message names the task, state and status — never "retry" ──


def test_no_progress_deadline_reports_task_id_state_and_last_status_not_retry(patched):
    """An identical WORKING heartbeat is not progress, so the no-progress bound still trips —
    but the failure now names the peer task id, the last state and status message, and how to
    resume/collect it, and never says to retry (r2)."""
    _clock(patched, step=0.3)
    _install_peer(patched, send=_working(msg="crunching numbers"), gets=_script([_working(msg="crunching numbers")]))

    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(_parse(poll_timeout_s=1), "do the long thing"))

    msg = str(ei.value)
    assert "without observable progress" in msg
    assert "task t1" in msg
    assert "state=TASK_STATE_WORKING" in msg
    assert "crunching numbers" in msg  # the peer's last status message rides back
    assert "resume_task_id='t1'" in msg
    assert "poll_timeout_s" in msg  # the configurable bound is named
    assert "retry" not in msg.lower()  # never tells the caller to re-send


def test_explicit_timeout_deadline_also_carries_the_task_id_and_no_retry(patched):
    """A per-call ``timeout`` caps a peer that keeps progressing; that message, too, names the
    task id and how to resume it, and never says retry (r2)."""
    _clock(patched, step=0.5)  # small enough that the poll loop runs before the 3s cap
    state = _install_peer(
        patched,
        send=_working(msg="phase 0"),
        # Distinct status each poll ⇒ the no-progress bound never trips; the explicit timeout does.
        gets=lambda n: _working(msg=f"phase {n + 1}"),
    )

    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(_parse(poll_timeout_s=300), "run the migration", timeout=3))

    msg = str(ei.value)
    assert "this call's timeout" in msg
    assert "task t1" in msg
    assert "resume_task_id='t1'" in msg
    assert "retry" not in msg.lower()
    assert state["methods"].count("GetTask") >= 1


# ── r3: a background delegation delivers a late completion as its result ────────


def test_background_delegation_delivers_a_completion_after_the_deadline(patched):
    """The core fix: a detached delegation whose peer finishes AFTER ``poll_timeout_s`` keeps
    polling the peer's own task and returns its REAL reply as the job result, instead of a
    lost FAILED with no task id (r3)."""
    _clock(patched, step=1.0)
    state = _install_peer(
        patched,
        send=_working(msg="still going"),
        gets=_script([_working(msg="still going")] * 3 + [_completed("the finished analysis")]),
    )

    out = asyncio.run(_detached(lambda: A.dispatch(_parse(poll_timeout_s=1), "big background job")))

    assert out == "the finished analysis"
    # The completion could only have arrived through the extended poll, past the 1s deadline.
    assert state["methods"].count("GetTask") >= 4


def test_background_delegation_delivers_a_late_park_with_a_resume_handle(patched):
    """If the peer PARKS on a question after the deadline, the background job delivers the
    question and its resume handle — the lead can answer it — rather than losing it (r3)."""
    _clock(patched, step=1.0)
    _install_peer(
        patched,
        send=_working(msg="thinking"),
        gets=_script([_working(msg="thinking")] * 2 + [_parked("Which brand palette should I use?")]),
    )

    out = asyncio.run(_detached(lambda: A.dispatch(_parse(poll_timeout_s=1), "background design task")))

    assert "needs input" in out
    assert "Which brand palette should I use?" in out
    assert "resume_task_id='t1'" in out


def test_background_delegation_that_never_finishes_reports_the_task_id_not_retry(patched, monkeypatch):
    """When the peer is STILL working after the bounded extra window, the background job fails
    — but with the actionable, task-id-bearing message, never a bare failure and never
    "retry" (r3 / r2)."""
    _clock(patched, step=1.0)
    monkeypatch.setattr(late, "_COLLECT_MAX_S", 2.0)  # a tiny extra window for the test
    _install_peer(patched, send=_working(msg="phase 2 of 9"), gets=_script([_working(msg="phase 2 of 9")]))

    with pytest.raises(DelegateError) as ei:
        asyncio.run(_detached(lambda: A.dispatch(_parse(poll_timeout_s=1), "endless background job")))

    msg = str(ei.value)
    assert "task t1" in msg
    assert "phase 2 of 9" in msg  # the last status the extended poll observed
    assert "state=TASK_STATE_WORKING" in msg
    assert "resume_task_id='t1'" in msg
    assert "retry" not in msg.lower()


def test_background_delegation_surfaces_a_late_peer_failure_diagnostic(patched):
    """A peer whose task actually FAILS during the extra window surfaces its own diagnostic —
    not a misleading "still running" note (r3)."""
    _clock(patched, step=1.0)
    _install_peer(
        patched,
        send=_working(msg="building"),
        gets=_script([_working(msg="building")] * 2 + [_failed("OOM killed the worker")]),
    )

    with pytest.raises(DelegateError) as ei:
        asyncio.run(_detached(lambda: A.dispatch(_parse(poll_timeout_s=1), "doomed background job")))

    msg = str(ei.value)
    assert "failed" in msg and "OOM killed the worker" in msg
    assert "still running" not in msg


# ── late.collect_task: the reusable poll the background path leans on ───────────


def test_collect_task_returns_the_answer_when_the_task_completes(patched):
    _clock(patched, step=1.0)
    _install_peer(patched, send=_working(), gets=_script([_working(msg="wait")] * 2 + [_completed("done at last")]))

    outcome, text, state, status = asyncio.run(late.collect_task(_parse(), "t1"))

    assert (outcome, text) == (late.ANSWERED, "done at last")
    assert state == "TASK_STATE_COMPLETED"


def test_collect_task_gives_up_at_the_window_with_the_last_observation(patched, monkeypatch):
    """On a peer that never settles, ``collect_task`` gives up at the ceiling and hands back
    the last state and status message it saw — what the deadline message reports."""
    _clock(patched, step=1.0)
    monkeypatch.setattr(late, "_COLLECT_MAX_S", 2.0)
    _install_peer(patched, send=_working(), gets=_script([_working(msg="halfway there")]))

    outcome, text, state, status = asyncio.run(late.collect_task(_parse(), "t1"))

    assert outcome == late.FAILED and "still working" in text
    assert state == "TASK_STATE_WORKING"
    assert status == "halfway there"


# ── the status-message extractor the carried text comes from ───────────────────


def test_status_message_text_reads_only_the_status_message_not_artifacts():
    """The carried "last status message" must be the peer's progress narration only — never
    a task's artifacts (partial output), which is why the extractor reads status.message
    alone."""
    assert _status_message_text(_working(msg="on it").json()["result"]) == "on it"
    # A COMPLETED task's artifact text is an ANSWER, not a status message — not carried here.
    assert _status_message_text(_completed("the real answer").json()["result"]) == ""
    # A stateless task envelope carries no status message.
    assert _status_message_text({"task": {"id": "t1", "artifacts": [{"parts": [{"text": "x"}]}]}}) == ""
