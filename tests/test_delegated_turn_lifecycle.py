"""A turn DELEGATED over A2A announces itself on the member's bus (#4290).

The hub's Fleet Room reads each member's event bus. A member used to publish
``turn.started`` only for turns it fired itself (scheduler, background resume), so a
member working on a ``delegate_to`` turn never showed busy — the room only logged
"finished a turn" afterwards. Now:

- the executor's ``turn_started`` frame carries who asked (``caller``) and the ask, and
  its ``turn_ended`` frame — fired on EVERY exit — carries the terminal state;
- the host publishes ``turn.started`` / ``turn.finished`` for ``origin: "a2a"`` turns
  only (console turns stream themselves; server-fired turns already bracket their own),
  pairing a park leg's start with its resumed leg's finish so a consumer's count stays
  balanced.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import server.a2a as a2a
from a2a_impl.executor import set_progress_hook, set_terminal_hook
from tests._a2a_app import A2A_HEADERS, poll_terminal
from tests._a2a_app import build_app as _build_app


@pytest.fixture(autouse=True)
def _hooks():
    set_progress_hook(None)
    set_terminal_hook(None)
    yield
    set_progress_hook(None)
    set_terminal_hook(None)


def _delegated_body(text="summarize the repo", **extra_md):
    md = {"origin": "a2a", "trigger": "delegate_to", "caller": "hub", **extra_md}
    return {
        "jsonrpc": "2.0",
        "id": "peer-rpc",
        "method": "SendMessage",
        "params": {
            "metadata": md,
            "message": {"messageId": "peer-message", "role": "ROLE_USER", "parts": [{"text": text}]},
            "configuration": {"returnImmediately": True},
        },
    }


async def _run(stream, body=None):
    frames: list = []
    set_progress_hook(lambda ctx, task, frame: frames.append((ctx, task, frame)))
    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await c.post("/a2a", headers=A2A_HEADERS, json=body or _delegated_body())).json()["result"]["task"]
        final = await poll_terminal(c, task["id"])
    for _ in range(50):  # turn_ended fires from the producer's finally, after the terminal frame
        if any(f[2].get("phase") == "turn_ended" for f in frames):
            break
        await asyncio.sleep(0.02)
    return task, final, frames


def _phase(frames, phase):
    return [f for f in frames if f[2].get("phase") == phase]


# ── executor: the frames carry what the host needs ───────────────────────────


@pytest.mark.asyncio
async def test_turn_started_carries_the_caller_and_the_ask_and_turn_ended_the_state():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "the summary")

    task, final, frames = await _run(stream)
    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    ((ctx, tid, started),) = _phase(frames, "turn_started")
    assert (ctx, tid) == (task["contextId"], task["id"])
    assert started["origin"] == "a2a"
    assert started["caller"] == "hub"
    assert started["stimulus"] == "summarize the repo"
    assert started["private"] is False
    ((_, _, ended),) = _phase(frames, "turn_ended")
    assert ended["state"] == "completed" and ended["origin"] == "a2a"


@pytest.mark.asyncio
async def test_a_failed_turn_ends_failed():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("error", "boom")

    _, final, frames = await _run(stream)
    assert final["status"]["state"] == "TASK_STATE_FAILED"
    assert _phase(frames, "turn_ended")[0][2]["state"] == "failed"


@pytest.mark.asyncio
async def test_a_crashing_turn_ends_failed():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        raise RuntimeError("exploded")
        yield  # pragma: no cover — makes this an async generator

    _, final, frames = await _run(stream)
    assert final["status"]["state"] == "TASK_STATE_FAILED"
    assert _phase(frames, "turn_ended")[0][2]["state"] == "failed"


@pytest.mark.asyncio
async def test_a_parked_turn_ends_input_required():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("input_required", {"question": "Which repo?"})

    _, final, frames = await _run(stream)
    assert final["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert _phase(frames, "turn_ended")[0][2]["state"] == "input_required"


@pytest.mark.asyncio
async def test_a_canceled_turn_ends_canceled():
    gate = asyncio.Event()

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "working… ")
        await gate.wait()  # never set: only a CancelTask ends this turn
        yield ("done", "unreachable")

    frames: list = []
    set_progress_hook(lambda ctx, task, frame: frames.append((ctx, task, frame)))
    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await c.post("/a2a", headers=A2A_HEADERS, json=_delegated_body())).json()["result"]["task"]
        for _ in range(100):
            if _phase(frames, "turn_started"):
                break
            await asyncio.sleep(0.02)
        r = await c.post(
            "/a2a",
            headers=A2A_HEADERS,
            json={"jsonrpc": "2.0", "id": "c", "method": "CancelTask", "params": {"id": task["id"]}},
        )
        assert "error" not in r.json(), r.json()
        for _ in range(100):
            if _phase(frames, "turn_ended"):
                break
            await asyncio.sleep(0.02)
    assert _phase(frames, "turn_ended")[0][2]["state"] == "canceled"


@pytest.mark.asyncio
async def test_an_incognito_turn_marks_its_ask_private():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "ok")

    _, _, frames = await _run(stream, _delegated_body(incognito=True))
    assert _phase(frames, "turn_started")[0][2]["private"] is True


# ── host: the bus events ─────────────────────────────────────────────────────


@pytest.fixture
def published(monkeypatch):
    out: list = []
    monkeypatch.setattr(a2a._event_bus, "publish", lambda t, d=None, **kw: out.append((t, d)))
    # Keep the chat-progress bridge out of it: these frames are about the lifecycle pair.
    monkeypatch.setattr(a2a, "_publish_chat_progress", lambda *a, **k: None)
    return out


def _start(**over):
    return {
        "phase": "turn_started",
        "resumed": False,
        "origin": "a2a",
        "trigger": "delegate_to",
        "caller": "hub",
        "stimulus": "summarize the repo",
        "private": False,
        **over,
    }


def _topics(published, *names):
    return [(t, d) for t, d in published if t in names]


def test_a_delegated_turn_publishes_turn_started_with_who_and_what(published):
    a2a._a2a_progress("ctx-1", "task-1", _start())
    ((topic, data),) = _topics(published, "turn.started")
    assert data == {
        "session_id": "ctx-1",
        "task_id": "task-1",
        "origin": "a2a",
        "trigger": "delegate_to",
        "caller": "hub",
        "summary": "summarize the repo",
    }


@pytest.mark.parametrize(
    ("state", "ok"),
    [("completed", True), ("failed", False), ("canceled", None)],
)
def test_every_terminal_exit_publishes_turn_finished(published, state, ok):
    a2a._a2a_progress("ctx-1", "task-1", {"phase": "turn_ended", "origin": "a2a", "state": state})
    ((_, data),) = _topics(published, "turn.finished")
    assert data == {"session_id": "ctx-1", "task_id": "task-1", "origin": "a2a", "state": state, "ok": ok}


def test_the_summary_is_one_clipped_line(published):
    a2a._a2a_progress("ctx-1", "task-1", _start(stimulus="line one\n\nline two " + "x" * 400, caller="h" * 200))
    ((_, data),) = _topics(published, "turn.started")
    assert "\n" not in data["summary"]
    assert data["summary"].startswith("line one line two")
    assert len(data["summary"]) <= a2a._DELEGATED_SUMMARY_LIMIT
    assert data["summary"].endswith("…")
    assert len(data["caller"]) <= a2a._DELEGATED_CALLER_LIMIT


def test_a_private_turn_withholds_its_summary(published):
    a2a._a2a_progress("ctx-1", "task-1", _start(private=True))
    ((_, data),) = _topics(published, "turn.started")
    assert data["summary"] == ""
    assert data["caller"] == "hub"


def test_a_park_and_its_resume_publish_one_balanced_pair(published):
    """The park leg's start pairs with the RESUMED leg's finish: no `turn.finished` for the
    park (it isn't over — `turn.input_required` says so) and no second `turn.started` for
    the resume (`turn.resumed` says so), so a counting consumer stays balanced."""
    a2a._a2a_progress("ctx-1", "task-1", _start())
    a2a._a2a_progress("ctx-1", "task-1", {"phase": "input_required", "prompt": "Which repo?"})
    a2a._a2a_progress("ctx-1", "task-1", {"phase": "turn_ended", "origin": "a2a", "state": "input_required"})
    a2a._a2a_progress("ctx-1", "task-1", _start(resumed=True))
    a2a._a2a_progress("ctx-1", "task-1", {"phase": "turn_ended", "origin": "a2a", "state": "completed"})
    assert [t for t, _ in published] == [
        "turn.started",
        "turn.input_required",
        "turn.resumed",
        "turn.finished",
    ]


@pytest.mark.parametrize("origin", ["", "scheduler", "background-resume", "inbox"])
def test_other_origins_publish_no_lifecycle_pair(published, origin):
    """The console streams its own turns and server-fired turns bracket themselves —
    announcing either here would double it."""
    a2a._a2a_progress("ctx-1", "task-1", _start(origin=origin))
    a2a._a2a_progress("ctx-1", "task-1", {"phase": "turn_ended", "origin": origin, "state": "completed"})
    assert _topics(published, "turn.started", "turn.finished") == []


def test_a_publish_failure_never_reaches_the_executor(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(a2a._event_bus, "publish", _boom)
    monkeypatch.setattr(a2a, "_publish_chat_progress", lambda *a, **k: None)
    a2a._a2a_progress("ctx-1", "task-1", _start())  # must not raise
