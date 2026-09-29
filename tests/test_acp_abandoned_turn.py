"""An abandoned ACP turn is stopped before the runtime is released (#3837).

``server.chat_acp._acp_drive_turn`` runs ``rt.run_turn`` in a driver task and yields
its frames from a queue. When the consumer walks away mid-turn (client disconnect, tab
close, A2A cancel → ``GeneratorExit`` / ``CancelledError``), the driver used to keep
running, orphaned, while the caller's ``finally`` released the runtime — so an idle,
evictable runtime still had a live prompt driving the external agent.

These run real asyncio against fake runtimes (and, for the wire, a real ``AcpClient``
talking to a scripted agent subprocess): the cancel must reach the runtime, and the
driver must be DONE before ``aclose()`` / the cancelled consumer returns — which is
what makes the callers' ``_acp_release`` happen after the turn has actually stopped.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import time
import types

import pytest

chat_acp = importlib.import_module("server.chat_acp")


class _HangingRuntime:
    """Streams one text delta, then works 'forever' until cancelled. Records the driver
    task it ran on and whether the cancel reached it."""

    def __init__(self, agent: str = "fake"):
        self.agent = agent
        self.task: asyncio.Task | None = None
        self.started = asyncio.Event()
        self.cancelled = False
        self.stopped = False  # set when run_turn has fully unwound

    async def run_turn(self, message, *, progress_callback=None, tool_callback=None, text_callback=None):
        self.task = asyncio.current_task()
        try:
            await text_callback("partial")
            self.started.set()
            await asyncio.sleep(3600)
            return "never"
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.stopped = True


def _driver_stopped(rt: _HangingRuntime) -> bool:
    return rt.task is not None and rt.task.done() and rt.stopped


async def test_aclose_after_first_frame_stops_the_driver_before_returning():
    """(a) Consume the first frame, then aclose() — the GeneratorExit path."""
    rt = _HangingRuntime()
    agen = chat_acp._acp_drive_turn(rt, "m")
    assert await agen.__anext__() == ("text", "partial")

    await agen.aclose()

    assert rt.cancelled, "the cancel never reached the runtime — the turn is still driving the agent"
    assert _driver_stopped(rt), "aclose() returned while the driver task was still running"
    assert rt.task.cancelled()


async def test_cancelling_the_consumer_mid_stream_stops_the_driver():
    """(b) Cancel the consuming task while it waits for the next frame."""
    rt = _HangingRuntime()
    got: list = []

    async def consume():
        async for frame in chat_acp._acp_drive_turn(rt, "m"):
            got.append(frame)

    consumer = asyncio.create_task(consume())
    await rt.started.wait()
    await asyncio.sleep(0)  # let the consumer take the frame and park on the queue
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert got == [("text", "partial")]
    assert rt.cancelled
    assert _driver_stopped(rt), "the consumer finished while the driver task was still running"


async def test_full_consumption_is_unchanged():
    """(c) A turn that runs to completion yields its frames, then usage, then done."""

    class _Rt:
        agent = "codex"

        async def run_turn(self, message, *, progress_callback=None, tool_callback=None, text_callback=None):
            await text_callback("Fixed ")
            await tool_callback({"phase": "start", "id": "e", "name": "edit", "input": "x"})
            await tool_callback({"phase": "end", "id": "e", "name": "edit", "output": "ok"})
            await text_callback("it.")
            return "Fixed it."

    frames = [f async for f in chat_acp._acp_drive_turn(_Rt(), "m")]

    assert frames == [
        ("text", "Fixed "),
        ("tool_start", {"id": "e", "name": "edit", "input": "x"}),
        ("tool_end", {"id": "e", "name": "edit", "output": "ok"}),
        ("text", "it."),
        (
            "usage",
            {
                "model": "acp:codex",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cost_usd": 0.0,
            },
        ),
        ("done", "Fixed it."),
    ]


async def test_a_runtime_that_ignores_the_cancel_is_bounded(monkeypatch, caplog):
    """A wedged runtime (swallows the cancel and keeps going) can't hold the release
    forever: the wait is bounded by ``_ACP_CANCEL_SETTLE_S`` and says so."""
    monkeypatch.setattr(chat_acp, "_ACP_CANCEL_SETTLE_S", 0.2)
    release = asyncio.Event()

    class _Stubborn:
        agent = "wedged"

        async def run_turn(self, message, *, progress_callback=None, tool_callback=None, text_callback=None):
            await text_callback("x")
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await release.wait()  # ignore the cancel
            return "late"

    agen = chat_acp._acp_drive_turn(_Stubborn(), "m")
    await agen.__anext__()
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        await agen.aclose()
    assert time.monotonic() - t0 < 2.0
    assert any("did not stop within" in r.getMessage() for r in caplog.records)
    release.set()  # let the stubborn task finish so the loop closes clean
    await asyncio.sleep(0.05)


async def test_collected_caller_releases_only_after_the_turn_stopped(monkeypatch):
    """The non-streaming caller: cancelling ``_acp_turn_collected`` mid-turn must stop the
    turn BEFORE ``_acp_release`` runs."""
    rt = _HangingRuntime()
    at_release: list[bool] = []

    async def fake_acquire(tid):
        return rt

    async def fake_release(tid):
        at_release.append(_driver_stopped(rt))

    monkeypatch.setattr(chat_acp, "_acp_acquire", fake_acquire)
    monkeypatch.setattr(chat_acp, "_acp_release", fake_release)

    t = asyncio.create_task(chat_acp._acp_turn_collected("s-3837-collected", "hi"))
    await rt.started.wait()
    await asyncio.sleep(0)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert at_release == [True], "runtime released while the abandoned turn was still running"


async def test_streaming_caller_releases_only_after_the_turn_stopped(monkeypatch):
    """The streaming caller (A2A / console): the consumer abandons the OUTER generator at
    its own ``yield``. A bare ``async for`` there leaves the inner drive generator to GC
    finalization — after the release — so the caller must close it explicitly."""
    import runtime.acp_runtime as acp_rt
    from graph.config import LangGraphConfig

    chat_mod = importlib.import_module("server.chat")
    turn_control = importlib.import_module("server.turn_control")

    class _Graph:  # STATE.graph must be set; the ACP switch never touches it
        pass

    monkeypatch.setattr(chat_mod.STATE, "graph", _Graph(), raising=False)
    monkeypatch.setattr(chat_mod.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(chat_mod.STATE, "graph_config", LangGraphConfig(), raising=False)

    async def _no_hold(*a, **k):
        return None

    monkeypatch.setattr(turn_control, "_hold_if_hitl_pending", _no_hold)
    monkeypatch.setattr(acp_rt, "is_acp_runtime", lambda cfg: True)

    rt = _HangingRuntime()
    at_release: list[bool] = []

    async def fake_acquire(tid):
        return rt

    async def fake_release(tid):
        at_release.append(_driver_stopped(rt))

    monkeypatch.setattr(chat_acp, "_acp_acquire", fake_acquire)
    monkeypatch.setattr(chat_acp, "_acp_release", fake_release)

    agen = chat_mod._chat_langgraph_stream_impl("hello", "s-3837-stream")
    assert await agen.__anext__() == ("text", "partial")
    await agen.aclose()

    assert at_release == [True], "runtime released while the abandoned turn was still running"
    assert rt.cancelled


# ── the wire: a real AcpClient tells the external agent to stop ────────────────

# Handshakes, then HANGS on session/prompt (never replies) while still reading stdin,
# so it can receive the session/cancel notification, which it records to a marker file.
# It streams one answer chunk first — long enough to clear the runtime's empty-reply
# buffer — so the test can abandon the turn after a frame, like a real disconnect.
_CANCEL_AGENT = r"""
import sys, json, os
MARKER = os.environ["CANCEL_MARKER"]
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1",
              "update": {"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": "working on it. " * 40}}}})  # past the empty-reply buffer
    elif method == "session/cancel":
        with open(MARKER, "w") as fh:
            fh.write("cancelled")
"""


class _Ctx:
    def assemble(self, *, query=""):
        from runtime.context import AssembledContext

        return AssembledContext(stable_prefix="", volatile_delta="", sources=[])

    def after_turn(self, *, user="", response=""):
        pass


async def test_abandoning_a_real_acp_turn_sends_session_cancel(tmp_path):
    """End to end over the real transport: aclose() on the drive generator cancels
    ``AcpRuntime.run_turn`` → ``AcpClient.prompt``, whose abort path sends ACP
    ``session/cancel`` before aclose() returns (the agent then only has to read its
    stdin to record it — nothing waits on the agent's reply)."""
    from plugins.coding_agent.acp_client import AcpClient
    from runtime.acp_runtime import AcpRuntime

    script = tmp_path / "cancel_agent.py"
    script.write_text(_CANCEL_AGENT, encoding="utf-8")
    marker = tmp_path / "cancelled.marker"
    client = AcpClient(
        sys.executable,
        [str(script)],
        cwd=str(tmp_path),
        name="cancel",
        env={"CANCEL_MARKER": str(marker)},
        record_runs=False,
    )
    cfg = types.SimpleNamespace(agent_runtime="acp:codex", operator_mcp_tools=["task_list"], acp_agents={})
    rt = AcpRuntime(cfg, cwd=str(tmp_path), client_factory=lambda: client, context=_Ctx())
    try:
        agen = chat_acp._acp_drive_turn(rt, "hang please")
        first = await asyncio.wait_for(agen.__anext__(), 30)
        assert first[0] == "text"
        await agen.aclose()
        assert not client._turn_lock.locked(), "the client still holds the abandoned turn"
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists(), "abandoning the turn never sent session/cancel to the agent"
    finally:
        await client.close()
