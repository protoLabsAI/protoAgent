"""Tool-fence rules for streaming dispatch, resumes and held messages (#1639/#2972).

The per-turn tool allowlist rides the graph state as ``subagent_fence``;
``SubagentFenceMiddleware`` enforces it. These pin four rules:

1. A fenced STREAMING turn skips every pre-turn short-circuit and is refused on an ACP
   runtime, as a non-streaming one is — except the background manager's own detached
   job, proven by the single-use in-process token its fire mints
   (``background/fire_auth.py``). Metadata alone (``origin``, a job id) proves nothing.
2. A fenced RESUME runs under the intersection of its fence and the parked turn's
   (narrowest wins); an empty intersection blocks every tool rather than unfencing.
3. The tool call that PARKED the turn completes on its own resume even when the
   resumer's fence excludes it — only that call, only on that pass.
4. A fenced message held behind a parked interrupt carries its fence: the pass that
   folds it in is narrowed to it, even when the resume itself is unfenced.

Unit tests fake the graph (``tests/_turn_driver_fakes``); the end-to-end ones drive the
REAL compiled graph with a scripted model so the middleware's enforcement is what's read.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from background import fire_auth
from graph import steering
from graph.middleware.steering import SteeringMiddleware
from graph.middleware.subagent_fence import FENCE_DENY_ALL, SubagentFenceMiddleware, intersect_fences
from tests._turn_driver_fakes import ScriptedGraph, text
from tests import test_turn_fence_every_pass as _every
from tests.test_turn_fence_every_pass import _call, _fence_of, _real_graph, _stream, _tool_messages

# The graph/STATE fixture those tests use — shared, not copied.
env = _every.env

chat_mod = importlib.import_module("server.chat")
chat_acp = importlib.import_module("server.chat_acp")
chat_dispatch = importlib.import_module("server.chat_dispatch")

_FENCE = ["discord_read"]


@pytest.fixture(autouse=True)
def _clean_queues():
    steering._QUEUES.clear()
    fire_auth._TOKENS.clear()
    yield
    steering._QUEUES.clear()
    fire_auth._TOKENS.clear()


# ── 1. streaming dispatch: short-circuits skipped, ACP refused, background exempt ──


@pytest.fixture
def acp(monkeypatch):
    """An ACP runtime whose drive is recorded instead of run."""
    import runtime.acp_runtime as acp_runtime

    ran: list[str] = []

    async def _acquire(tid):
        return object()

    async def _release(tid):
        return None

    async def _drive(rt, message):
        ran.append(message)
        yield ("done", "ran-on-acp")

    monkeypatch.setattr(acp_runtime, "is_acp_runtime", lambda cfg: True)
    monkeypatch.setattr(chat_acp, "_acp_acquire", _acquire)
    monkeypatch.setattr(chat_acp, "_acp_release", _release)
    monkeypatch.setattr(chat_acp, "_acp_drive_turn", _drive)
    return ran


def _bg_metadata(job_id: str, token: str | None, **extra) -> dict:
    md = {"origin": "background", "trigger": job_id, "background_job_id": job_id, "subagent_fence": _FENCE}
    if token is not None:
        md[fire_auth.METADATA_KEY] = token
    return {**md, **extra}


@pytest.mark.asyncio
async def test_stream_fenced_turn_skips_the_short_circuits(env):
    """`/foobar` on a fenced turn reaches the fenced lead turn verbatim — the unknown-
    command short-circuit (like every other one) doesn't run."""
    g = env.install(streams=[[text("r1", "ok")]])

    frames = await _stream("/foobar do it", request_metadata={"subagent_fence": _FENCE})

    assert frames[-1] == ("done", "ok")
    ((graph_input, _),) = g.stream_calls
    assert graph_input["messages"][-1].content == "/foobar do it"
    assert _fence_of(graph_input) == _FENCE


@pytest.mark.asyncio
async def test_stream_unfenced_turn_still_short_circuits(env):
    env.install()

    frames = await _stream("/foobar do it")

    assert frames[-1][0] == "done" and "Unknown command /foobar" in frames[-1][1]


@pytest.mark.asyncio
async def test_stream_fenced_turn_is_refused_on_an_acp_runtime(env, acp):
    env.install()

    frames = await _stream("hello", request_metadata={"subagent_fence": _FENCE})

    assert frames == [("done", chat_dispatch._FENCED_ACP_REFUSAL)]
    assert acp == []


@pytest.mark.asyncio
async def test_stream_own_background_fire_runs_on_an_acp_runtime(env, acp):
    env.install()
    token = fire_auth.mint("j1")

    frames = await _stream("[Background task] go", "background:j1", request_metadata=_bg_metadata("j1", token))

    assert frames == [("done", "ran-on-acp")]
    assert acp == ["[Background task] go"]
    assert "j1" not in fire_auth._TOKENS  # single use: redeemed at turn start


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["no-token", "forged-token", "other-jobs-token", "wrong-context", "replayed"],
)
async def test_stream_background_lookalike_is_refused_on_an_acp_runtime(env, acp, case):
    """Everything a remote A2A caller can put in metadata — origin, job id, a token — is
    refused unless the token is the one this process minted for that job, in that job's
    context, not yet redeemed."""
    env.install()
    token = fire_auth.mint("j1")
    other = fire_auth.mint("j2")
    session = "background:j1"
    md = {
        "no-token": _bg_metadata("j1", None),
        "forged-token": _bg_metadata("j1", "x" * len(token)),
        "other-jobs-token": _bg_metadata("j1", other),
        "wrong-context": _bg_metadata("j1", token),
        "replayed": _bg_metadata("j1", token),
    }[case]
    if case == "wrong-context":
        session = "s-remote"
    if case == "replayed":
        assert fire_auth.redeem(_bg_metadata("j1", token), "background:j1")

    frames = await _stream("[Background task] go", session, request_metadata=md)

    assert frames == [("done", chat_dispatch._FENCED_ACP_REFUSAL)]
    assert acp == []


@pytest.mark.asyncio
async def test_stream_own_background_fire_still_skips_the_short_circuits(env, acp):
    """The exemption is from the ACP refusal ONLY: the fired text never meets a
    short-circuit (a `/command` goes to the turn verbatim)."""
    env.install()
    token = fire_auth.mint("j1")

    frames = await _stream("/foobar", "background:j1", request_metadata=_bg_metadata("j1", token))

    assert frames == [("done", "ran-on-acp")]
    assert acp == ["/foobar"]


@pytest.mark.asyncio
async def test_background_fire_mints_a_token_only_for_a_fenced_job_and_drops_it_after(monkeypatch):
    from background.manager import BackgroundManager

    sent: list[dict] = []

    async def _send(self, *, context_id, text, metadata):
        sent.append({"context_id": context_id, "metadata": dict(metadata)})
        # While the POST is in flight the token is redeemable for exactly this context.
        tok = metadata.get(fire_auth.METADATA_KEY)
        if tok:
            assert fire_auth._TOKENS.get("j1") == tok
        return "task-1"

    monkeypatch.setattr(BackgroundManager, "_send_a2a_message", _send)
    mgr = BackgroundManager.__new__(BackgroundManager)
    import asyncio

    mgr._sem = asyncio.Semaphore(1)
    mgr.store = SimpleNamespace(mark_complete=lambda *a, **k: None)

    await mgr._fire("j1", "prompt", ["web_search"])
    await mgr._fire("j2", "prompt", None)

    fenced, unfenced = sent
    assert fenced["context_id"] == "background:j1"
    assert len(fenced["metadata"][fire_auth.METADATA_KEY]) >= 40
    assert fire_auth.METADATA_KEY not in unfenced["metadata"]
    assert fire_auth._TOKENS == {}  # dropped once the POST returned


# ── 2. a fenced resume intersects with the parked fence ─────────────────────────


class _ParkedFenceGraph(ScriptedGraph):
    def __init__(self, parked_fence, **kw):
        super().__init__(**kw)
        self.parked_fence = parked_fence

    async def aget_state(self, config):
        snap = await super().aget_state(config)
        snap.values = {"subagent_fence": self.parked_fence} if self.parked_fence else {}
        return snap


def _install_parked(env, monkeypatch, parked_fence, **kw):
    import runtime.state as rs

    g = _ParkedFenceGraph(parked_fence, **kw)
    g.pending.append({"question": "Which env?"})
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    env.graph = g
    return g


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parked", "resumer", "expected"),
    [
        (["ask_human"], ["ask_human", "current_time"], ["ask_human"]),
        (["ask_human", "current_time"], ["current_time"], ["current_time"]),
        (["ask_human"], ["current_time"], [FENCE_DENY_ALL]),
        (None, ["current_time"], ["current_time"]),
    ],
)
async def test_stream_fenced_resume_intersects_the_parked_fence(env, monkeypatch, parked, resumer, expected):
    g = _install_parked(env, monkeypatch, parked, streams=[[text("r1", "ok")]])

    await _stream("staging", resume=True, request_metadata={"subagent_fence": resumer})

    ((graph_input, _),) = g.stream_calls
    assert isinstance(graph_input, Command)
    assert _fence_of(graph_input) == expected


@pytest.mark.asyncio
async def test_sync_fenced_resume_intersects_the_parked_fence(env, monkeypatch):
    from tests._turn_driver_fakes import turn_result

    g = _install_parked(env, monkeypatch, ["ask_human"], invokes=[turn_result(AIMessage(content="ok"))])

    await chat_mod.chat("staging", "s1", hitl_resume=True, tool_fence=["ask_human", "current_time"])

    ((graph_input, _),) = g.invoke_calls
    assert _fence_of(graph_input) == ["ask_human"]


@pytest.mark.asyncio
async def test_unreadable_parked_fence_fails_closed(env, monkeypatch):
    from server import turn_stream

    class _Broken:
        async def aget_state(self, config):
            raise RuntimeError("checkpointer down")

    monkeypatch.setattr(env.state, "graph", _Broken(), raising=False)

    assert await turn_stream._resume_fence_update({}, ["x"]) == {"update": {"subagent_fence": [FENCE_DENY_ALL]}}
    assert await turn_stream._resume_fence_update({}, None) == {}


def test_intersect_fences_rules():
    assert intersect_fences(None, None) == []
    assert intersect_fences([], ["a"]) == ["a"]
    assert intersect_fences(["a"], []) == ["a"]
    assert intersect_fences(["a", "b"], ["b", "c"]) == ["b"]
    assert intersect_fences(["a"], ["b"]) == [FENCE_DENY_ALL]
    assert intersect_fences([FENCE_DENY_ALL], ["a"]) == [FENCE_DENY_ALL]


def test_deny_all_fence_blocks_every_tool():
    mw = SubagentFenceMiddleware()
    req = SimpleNamespace(tool_call={"name": "anything", "id": "c1"}, state={"subagent_fence": [FENCE_DENY_ALL]})

    out = mw.wrap_tool_call(req, lambda r: ToolMessage(content="ran", tool_call_id="c1"))

    assert out.status == "error" and "allows no tools" in out.content


@pytest.mark.asyncio
async def test_e2e_fenced_resume_cannot_widen_a_parked_fenced_turn(env, monkeypatch):
    g = _real_graph(
        monkeypatch,
        [_call("ask_human", "q1", {"question": "Which env?"}), _call("current_time", "c1"), AIMessage(content="done")],
    )

    parked = await _stream("deploy", "sW", request_metadata={"subagent_fence": ["ask_human"]})
    assert parked[-1][0] == "input_required"

    await _stream("staging", "sW", resume=True, request_metadata={"subagent_fence": ["ask_human", "current_time"]})

    tools = await _tool_messages(g, "a2a:sW")
    assert [t.tool_call_id for t in tools] == ["q1", "c1"]
    assert tools[0].status != "error" and tools[0].content == "staging"
    assert tools[1].status == "error" and "Blocked by policy" in tools[1].content


# ── 3. the parked call completes on its own resume ──────────────────────────────


@pytest.mark.asyncio
async def test_e2e_parked_call_completes_on_a_resume_whose_fence_excludes_it(env, monkeypatch):
    """The operator's answer isn't dropped: the parked ask_human returns it. The next
    call in the same pass — another ask_human included — is fenced as usual."""
    g = _real_graph(
        monkeypatch,
        [
            _call("ask_human", "q1", {"question": "Which env?"}),
            _call("ask_human", "q2", {"question": "Sure?"}),
            _call("current_time", "c1"),
            AIMessage(content="done"),
        ],
    )

    parked = await _stream("deploy", "sP")
    assert parked[-1][0] == "input_required"

    frames = await _stream("staging", "sP", resume=True, request_metadata={"subagent_fence": _FENCE})

    assert frames[-1][0] == "done"
    q1, q2, c1 = await _tool_messages(g, "a2a:sP")
    assert (q1.tool_call_id, q1.status, q1.content) == ("q1", "success", "staging")
    assert q2.status == "error" and "Blocked by policy" in q2.content
    assert c1.status == "error" and "Blocked by policy" in c1.content


@pytest.mark.asyncio
async def test_e2e_fenced_fresh_turn_cannot_park_on_a_tool_outside_its_fence(env, monkeypatch):
    """The exemption is for a RESUME only: a fenced turn's own out-of-fence ask_human is
    blocked, never parked."""
    g = _real_graph(monkeypatch, [_call("ask_human", "q1", {"question": "?"}), AIMessage(content="done")])

    frames = await _stream("go", "sF", request_metadata={"subagent_fence": _FENCE})

    assert frames[-1][0] == "done"
    (q1,) = await _tool_messages(g, "a2a:sF")
    assert q1.status == "error" and "Blocked by policy" in q1.content


# ── 4. a held fenced message carries its fence into the pass that reads it ────────


def test_steering_fold_narrows_the_pass_to_a_held_messages_fence():
    steering.enqueue("s", "plain", msg_id="a")
    steering.enqueue("s", "relayed", msg_id="b", fence=["x", "y"])

    update, queued = SteeringMiddleware._drain({"session_id": "s", "subagent_fence": ["y", "z"]})

    assert update["subagent_fence"] == ["y"]
    assert [q["id"] for q in queued] == ["a", "b"]
    assert all("fence" not in q for q in queued)  # the UI marker carries no enforcement state


def test_steering_fold_of_unfenced_messages_leaves_the_fence_alone():
    steering.enqueue("s", "plain", msg_id="a")

    update, _ = SteeringMiddleware._drain({"session_id": "s"})

    assert "subagent_fence" not in update
    assert steering.pending_items("s") == []


def test_steering_fold_into_an_unfenced_pass_takes_the_messages_fence():
    steering.enqueue("s", "relayed", msg_id="b", fence=["x"])

    update, _ = SteeringMiddleware._drain({"session_id": "s"})

    assert update["subagent_fence"] == ["x"]


@pytest.mark.asyncio
async def test_stream_held_fenced_message_is_queued_with_its_fence(env):
    g = env.install()
    g.pending.append({"question": "Which env?"})

    frames = await _stream("relayed text", "sQ", request_metadata={"subagent_fence": _FENCE})

    assert frames[-1][0] == "input_required"
    (item,) = steering.pending_items("sQ")
    assert item["text"] == "relayed text" and item["fence"] == _FENCE


@pytest.mark.asyncio
async def test_sync_held_fenced_message_is_queued_with_its_fence(env):
    g = env.install()
    g.pending.append({"question": "Which env?"})

    await chat_mod.chat("relayed text", "sQ", tool_fence=_FENCE)

    (item,) = steering.pending_items("sQ")
    assert item["fence"] == _FENCE


@pytest.mark.asyncio
async def test_e2e_held_fenced_message_fences_the_unfenced_resume_that_reads_it(env, monkeypatch):
    g = _real_graph(
        monkeypatch,
        [_call("ask_human", "q1", {"question": "Which env?"}), _call("current_time", "c1"), AIMessage(content="done")],
    )

    parked = await _stream("deploy", "sH")
    assert parked[-1][0] == "input_required"
    held = await _stream("relayed: check the time", "sH", request_metadata={"subagent_fence": _FENCE})
    assert held[-1][0] == "input_required"

    await _stream("staging", "sH", resume=True)

    q1, c1 = await _tool_messages(g, "a2a:sH")
    assert q1.status == "success" and q1.content == "staging"
    assert c1.status == "error" and "Blocked by policy" in c1.content
    snap = await g.aget_state({"configurable": {"thread_id": "a2a:sH"}})
    assert any("relayed: check the time" in str(m.content) for m in snap.values["messages"])
