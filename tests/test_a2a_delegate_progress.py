"""An ``a2a`` delegation's LIVE progress on the delegation card (#3979, A2A transport).

The ACP half feeds ``graph.delegate_progress`` from a coder's ``session/update``s. This is
the A2A half: when a peer's agent card advertises streaming, the adapter follows the task
it handed over on ``SubscribeToTask`` (SSE) and translates the peer's frames — tool-call-v1
extension frames, status text, a nested delegation's plan, produced artifacts — into the
same three feeds. Never the answer artifact's text: that is the reply, the chat's to render. The poll still owns the answer; the stream only observes (and wakes the poll
early when it sees the task settle).

Pinned two ways: the frame translation on crafted frames, and the whole path against a
REAL A2A server (a2a-sdk + ``ProtoAgentExecutor``, served by uvicorn on a free port) whose
turn emits tool calls and streams its answer — exactly what a protoAgent peer sends.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest

from graph import delegate_progress as dp
from plugins.delegates.a2a_progress import TOOL_CALL_EXT_URI, A2AProgressFeed, peer_streams

pytestmark = pytest.mark.platform_sensitive  # binds a socket, serves on a thread


class _Recorder:
    """A tracker stand-in that records the normalized feeds."""

    def __init__(self):
        self.calls: list = []
        self.text = ""

    async def on_tool(self, event):
        self.calls.append(("tool", event))

    async def on_text(self, delta):
        self.text += delta
        self.calls.append(("text", delta))

    async def on_plan(self, entries):
        self.calls.append(("plan", entries))


def _status(state="TASK_STATE_WORKING", *, meta=None, parts=None):
    message = {"role": "ROLE_AGENT", "parts": parts or []}
    if meta:
        message["metadata"] = meta
    return {"statusUpdate": {"taskId": "t", "status": {"state": state, "message": message}}}


def _tool(phase, tid, name, args=None):
    payload = {"toolCallId": tid, "name": name, "phase": phase}
    if args is not None:
        payload["args"] = args
    return {TOOL_CALL_EXT_URI: payload}


# ── translation ──────────────────────────────────────────────────────────────────


def test_peer_streams_reads_the_card_capability():
    assert peer_streams({"capabilities": {"streaming": True}}) is True
    assert peer_streams({"capabilities": {"streaming": False}}) is False
    assert peer_streams({}) is False and peer_streams(None) is False


async def test_tool_call_frames_become_tool_events_with_a_location_from_the_args():
    rec = _Recorder()
    feed = A2AProgressFeed(rec)
    await feed.frame(_status(meta=_tool("started", "c1", "read_file", "")))
    # protoAgent announces a call twice: early (no args), then with them — a refinement.
    await feed.frame(_status(meta=_tool("started", "c1", "read_file", json.dumps({"path": "src/calc.py"}))))
    await feed.frame(_status(meta=_tool("completed", "c1", "read_file")))
    await feed.frame(_status(meta=_tool("failed", "c2", "run_command")))
    tools = [e for kind, e in rec.calls if kind == "tool"]
    assert [(e["phase"], e["id"]) for e in tools] == [("start", "c1"), ("update", "c1"), ("end", "c1"), ("end", "c2")]
    assert tools[1]["locations"] == [{"path": "src/calc.py"}]
    assert tools[3]["status"] == "failed"


async def test_status_text_plan_and_produced_artifacts_map_but_answer_text_never_does():
    rec = _Recorder()
    feed = A2AProgressFeed(rec)
    await feed.frame(_status(parts=[{"text": "Looking at the repo."}]))
    nested = {"id": "x", "target": "coder", "plan": [{"content": "Read", "status": "completed"}]}
    await feed.frame(
        _status(parts=[{"data": nested, "metadata": {"mimeType": "application/vnd.protolabs.delegate-progress-v1+json"}}])
    )
    # The answer artifact streaming in (first chunk, appends, the terminal re-send): the
    # reply itself — the chat renders it; the card never sees a word of it.
    await feed.frame({"artifactUpdate": {"artifact": {"artifactId": "ans", "parts": [{"text": "All"}]}}})
    await feed.frame({"artifactUpdate": {"append": True, "artifact": {"artifactId": "ans", "parts": [{"text": " Done"}]}}})
    await feed.frame({"artifactUpdate": {"append": False, "artifact": {"artifactId": "ans", "parts": [{"text": "All Done"}]}}})
    await feed.frame({"artifactUpdate": {"artifact": {"name": "report.pdf", "artifactId": "a1", "parts": [{"url": "x"}]}}})
    assert rec.text == "Looking at the repo."
    assert ("plan", nested["plan"]) in rec.calls
    produced = [e for kind, e in rec.calls if kind == "tool" and e["id"] == "artifact:a1"]
    assert [e["phase"] for e in produced] == ["start", "end"] and produced[0]["name"] == "produced report.pdf"
    assert not feed.settled.is_set()


async def test_an_answer_artifact_stream_puts_no_text_on_the_card_but_tools_and_plan_still_do(monkeypatch):
    """End to end through the real tracker: a peer that narrates in WORKING status text,
    calls a tool, reports a nested plan, then streams its answer artifact. The card shows
    the narration (a tool call followed it), the tool and the plan — never the answer."""
    sent = []

    async def sink(snap):
        sent.append(snap)

    tracker = dp.DelegateProgress("peer", sink, min_interval=0.0)
    feed = A2AProgressFeed(tracker)
    await feed.frame(_status(parts=[{"text": "Checking the tests."}]))
    await feed.frame(_status(meta=_tool("started", "c1", "run_command")))
    nested = {"id": "x", "target": "coder", "plan": [{"content": "Run tests", "status": "in_progress"}]}
    await feed.frame(
        _status(parts=[{"data": nested, "metadata": {"mimeType": "application/vnd.protolabs.delegate-progress-v1+json"}}])
    )
    await feed.frame(_status(meta=_tool("completed", "c1", "run_command")))
    await feed.frame(_status(parts=[{"text": "Summing up."}]))  # status text no tool follows
    for i, word in enumerate(("All ", "three ", "tests ", "pass.")):
        await feed.frame({"artifactUpdate": {"append": i > 0, "artifact": {"artifactId": "ans", "parts": [{"text": word}]}}})
    await feed.frame(_status("TASK_STATE_COMPLETED", parts=[{"text": "All three tests pass."}]))
    await tracker.finish(ok=True)
    assert feed.settled.is_set()
    assert not any("pass" in s["text"] or "Summing" in s["text"] for s in sent)
    final = sent[-1]
    assert final["done"] is True and final["text"] == "Checking the tests."
    assert [t["name"] for t in final["recent_tools"]] == ["run_command"]
    assert final["plan"] == [{"content": "Run tests", "status": "in_progress"}]


@pytest.mark.parametrize("state", ["TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"])
async def test_a_settled_task_wakes_the_poll(state):
    feed = A2AProgressFeed(_Recorder())
    await feed.frame(_status(state, parts=[{"text": "a question?"}]))
    assert feed.settled.is_set()


async def test_the_subscription_snapshot_replays_tools_made_before_it_opened():
    rec = _Recorder()
    feed = A2AProgressFeed(rec)
    history = [
        {"role": "ROLE_USER", "parts": [{"text": "go"}]},
        {"role": "ROLE_AGENT", "parts": [], "metadata": _tool("started", "c0", "list_dir")},
    ]
    await feed.frame({"task": {"id": "t", "status": {"state": "TASK_STATE_WORKING"}, "history": history}})
    assert [e["name"] for kind, e in rec.calls if kind == "tool"] == ["list_dir"]


# ── a real A2A peer ───────────────────────────────────────────────────────────────


def _serve_peer(stream_fn, *, streaming=True):
    """A real a2a-sdk server driven by ``ProtoAgentExecutor(stream_fn)`` on a free port."""
    import protolabs_a2a as pa
    import uvicorn
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
    from a2a.types import AgentSkill
    from fastapi import FastAPI

    from a2a_impl.executor import ProtoAgentExecutor
    from a2a_impl.registry import harden_active_task_registry

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/a2a"
    card = pa.build_agent_card(
        name="peer",
        description="d",
        url=url,
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="Chat", description="chat", tags=["t"])],
        bearer=False,
        streaming=streaming,
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )
    harden_active_task_registry(handler)
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    return url, server, thread


async def _peer_turn(text, ctx, *, resume=False, caller_trace=None, **kwargs):
    """What a protoAgent peer's turn sends: tool cards (announced twice), then the answer."""
    yield ("tool_start", {"id": "c1", "name": "read_file", "input": ""})
    yield ("tool_start", {"id": "c1", "name": "read_file", "input": json.dumps({"path": "src/calc.py"})})
    await asyncio.sleep(0.4)
    yield ("tool_end", {"id": "c1", "name": "read_file", "output": "def add(a, b): ..."})
    yield ("tool_start", {"id": "c2", "name": "run_command", "input": json.dumps({"command": "pytest -q"})})
    await asyncio.sleep(0.4)
    yield ("tool_end", {"id": "c2", "name": "run_command", "output": "3 passed"})
    for word in ("All ", "three ", "tests ", "pass."):
        yield ("text", word)
        await asyncio.sleep(0.05)
    yield ("done", "All three tests pass.")


@pytest.fixture
def peer():
    servers = []

    def start(stream_fn=_peer_turn, **kw):
        url, server, thread = _serve_peer(stream_fn, **kw)
        servers.append((server, thread))
        return url

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(5)


def _delegate(url):
    from plugins.delegates.a2a import A2aAdapter

    adapter = A2aAdapter()
    return adapter, adapter.parse({"name": "peer", "type": "a2a", "url": url, "poll_timeout_s": 30})


async def test_a_streaming_peer_reports_its_tools_but_not_its_answer_into_the_card(peer, monkeypatch):
    monkeypatch.setattr(dp, "MIN_INTERVAL_S", 0.0)
    adapter, d = _delegate(peer())
    snaps = []

    async def sink(snap):
        snaps.append(snap)

    with dp.progress_sink(sink):
        reply = await adapter.dispatch(d, "run the tests")

    assert reply == "All three tests pass."
    assert snaps, "no progress reached the card"
    names = {t["name"] for s in snaps for t in s["recent_tools"]}
    assert {"read_file", "run_command"} <= names
    read = next(t for t in snaps[-1]["recent_tools"] if t["name"] == "read_file")  # refined in place
    assert read["locations"] == [{"path": "src/calc.py"}]
    final = snaps[-1]
    assert final["done"] is True and final["ok"] is True and final["target"] == "peer"
    assert final["tool_count"] == 2
    assert all(t["status"] == "completed" for t in final["recent_tools"])
    # The answer streamed into the peer's answer artifact; the chat renders it — the card
    # never showed a word of it, live or in the done snapshot.
    assert all(s["text"] == "" for s in snaps)


async def test_a_non_streaming_peer_keeps_todays_behaviour(peer):
    adapter, d = _delegate(peer(streaming=False))
    snaps = []

    async def sink(snap):
        snaps.append(snap)

    with dp.progress_sink(sink):
        reply = await adapter.dispatch(d, "run the tests")
    assert reply == "All three tests pass."
    assert snaps == []  # no tracker: the card keeps its spinner and the final reply


async def test_no_sink_no_subscription(peer, monkeypatch):
    """A caller that owns no card (a CLI, the board, a test) dispatches exactly as before."""
    from plugins.delegates import a2a_progress

    started = []
    monkeypatch.setattr(a2a_progress.LiveView, "start", lambda self, *a: started.append(a))
    adapter, d = _delegate(peer())
    assert await adapter.dispatch(d, "run the tests") == "All three tests pass."
    assert started == []


async def test_the_mention_path_streams_an_a2a_peers_progress_to_its_card(peer, monkeypatch):
    """`@peer <task>` through the real pre-turn chain: progress frames keyed to the mention
    card arrive before the card's end and the peer's reply."""
    import importlib

    from plugins.delegates.registry import DelegateRegistry
    from runtime.state import STATE

    chat_dispatch = importlib.import_module("server.chat_dispatch")
    reg = DelegateRegistry([{"name": "peer", "type": "a2a", "url": peer(), "poll_timeout_s": 30}])
    monkeypatch.setattr(STATE, "delegate_registry", reg, raising=False)
    monkeypatch.setattr(STATE, "graph", None, raising=False)
    pre = chat_dispatch._PreTurn("@peer run the tests")
    frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "sess-a2a", None)]
    kinds = [k for k, _ in frames]
    progress = [p for k, p in frames if k == "delegate_progress"]
    assert progress and all(p["id"] == "mention:peer" for p in progress)
    assert kinds.index("delegate_progress") < kinds.index("tool_end")
    assert progress[-1]["done"] is True and progress[-1]["tool_count"] == 2
    reply = next(p for k, p in frames if k == "room_reply")
    assert reply["text"] == "All three tests pass."
