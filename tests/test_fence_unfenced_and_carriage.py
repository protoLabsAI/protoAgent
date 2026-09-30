"""Unfenced turns start unfenced; work a fenced turn leaves behind stays fenced (#1639/#2972).

The per-turn tool allowlist rides the graph state as ``subagent_fence`` and the channel
persists per checkpointer thread. These pin:

E. Every fresh pass stamps its fence explicitly — an UNFENCED streaming pass stamps
   ``[]`` (as incognito, and as the non-streaming driver always did), so a plain
   operator turn never inherits a fence a previous turn (or a fenced message folded into
   one) left on the thread. Server-fired turns that must keep their ORIGIN's fence carry
   it explicitly instead of relying on that inheritance: a background job's push-resume
   nudge (single and batch), a scheduler fire (``wait`` resume, scheduled one-shot,
   ``run_in_session``), a watch reaction and a goal's completion hooks all record the
   creating turn's fence (``graph.fence_scope``, opened by ``SubagentFenceMiddleware``
   around every tool call) and fire under it.
D. A fenced message folded into a pass NARROWS the rest of that turn: a goal
   continuation / the context-overflow retry keeps the narrowed fence on BOTH drivers
   (``server.turn_stream._carried_fence``), never re-stamps the turn's original one.

End-to-end tests drive the REAL compiled graph with a scripted model, so what's read is
the middleware's enforcement.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from graph import steering
from graph.config import LangGraphConfig
from graph.fence_scope import current_fence, fence_scope, normalize_fence
from graph.middleware.subagent_fence import FENCE_DENY_ALL
from tests import test_turn_fence_every_pass as _every
from tests._turn_driver_fakes import FakeGoals, text
from tests.test_turn_fence_every_pass import _call, _fence_of, _stream, _tool_messages, _ToolFake

env = _every.env

chat_mod = importlib.import_module("server.chat")
turn_stream = importlib.import_module("server.turn_stream")

_FENCE = ["discord_read"]
_OVERFLOW = "Error code: 400 - This model's maximum context length is 128000 tokens."


@pytest.fixture(autouse=True)
def _clean_queues():
    steering._QUEUES.clear()
    yield
    steering._QUEUES.clear()


def _graph(monkeypatch, model):
    from unittest.mock import patch

    import runtime.state as rs
    from langgraph.checkpoint.memory import MemorySaver

    with patch("graph.agent.create_llm", lambda *a, **k: model):
        from graph.agent import create_agent_graph

        g = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    return g


def _real_graph(monkeypatch, messages):
    return _graph(monkeypatch, _ToolFake(messages=iter(messages), disable_streaming=True))


_RAISED: list[bool] = []


class _OverflowFirst(_ToolFake):
    """Raises a context-overflow error on its FIRST call (after the pass's steering fold
    has landed on the checkpoint), then answers from its script."""

    def _generate(self, *args, **kwargs):
        if not _RAISED:
            _RAISED.append(True)
            raise RuntimeError(_OVERFLOW)
        return super()._generate(*args, **kwargs)


async def _fence_on(graph, thread_id):
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return snap.values.get("subagent_fence")


def _assert_blocked(tool, name="current_time"):
    assert tool.status == "error"
    assert "Blocked by policy" in tool.content and name in tool.content


def _assert_ran(tool):
    assert tool.status == "success", tool.content
    assert "Blocked by policy" not in tool.content


# ── E: an unfenced streaming turn starts unfenced ────────────────────────────


@pytest.mark.asyncio
async def test_stream_unfenced_fresh_pass_stamps_an_empty_fence(env):
    g = env.install(streams=[[text("r1", "ok")]])

    await _stream("hello")

    assert _fence_of(g.stream_calls[0][0]) == []


@pytest.mark.asyncio
async def test_e2e_plain_turn_after_a_fenced_turn_runs_unfenced(env, monkeypatch):
    g = _real_graph(monkeypatch, [AIMessage(content="ok"), _call("current_time", "c1"), AIMessage(content="done")])

    await _stream("relayed", "sP", request_metadata={"subagent_fence": _FENCE})
    assert await _fence_on(g, "a2a:sP") == _FENCE

    frames = await _stream("what time is it?", "sP")

    assert frames[-1][0] == "done"
    (tool,) = await _tool_messages(g, "a2a:sP")
    _assert_ran(tool)
    assert await _fence_on(g, "a2a:sP") == []


@pytest.mark.asyncio
async def test_e2e_plain_turn_after_a_folded_fenced_message_runs_unfenced(env, monkeypatch):
    """The reproduction: a fenced message held for the thread folds into an operator
    turn (narrowing THAT turn); the operator's NEXT plain turn must not stay fenced."""
    g = _real_graph(monkeypatch, [AIMessage(content="ok"), _call("current_time", "c1"), AIMessage(content="done")])
    steering.enqueue("sF", "relayed text", msg_id="m1", fence=_FENCE)

    await _stream("hello", "sF")
    assert await _fence_on(g, "a2a:sF") == _FENCE  # the fold narrowed that turn

    await _stream("what time is it?", "sF")

    (tool,) = await _tool_messages(g, "a2a:sF")
    _assert_ran(tool)


# ── D: a narrowing folded into a pass holds for the rest of the turn ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True], ids=["same-thread", "fresh-context"])
async def test_e2e_sync_goal_continuation_keeps_the_folded_narrowing(env, monkeypatch, fresh):
    goals = FakeGoals([("continue", "again", "iterate"), ("done", "met")], iteration=3, fresh=fresh)
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = _real_graph(monkeypatch, [AIMessage(content="one"), _call("current_time", "c1"), AIMessage(content="two")])
    steering.enqueue("sD", "relayed text", msg_id="m1", fence=_FENCE)

    await chat_mod.chat("go", "sD")

    thread = "a2a:sD:goal-iter-4" if fresh else "a2a:sD"
    (tool,) = await _tool_messages(g, thread)
    _assert_blocked(tool)


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True], ids=["same-thread", "fresh-context"])
async def test_e2e_stream_goal_continuation_keeps_the_folded_narrowing(env, monkeypatch, fresh):
    goals = FakeGoals([("continue", "again", "iterate"), ("done", "met")], iteration=3, fresh=fresh)
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = _real_graph(monkeypatch, [AIMessage(content="one"), _call("current_time", "c1"), AIMessage(content="two")])
    steering.enqueue("sS", "relayed text", msg_id="m1", fence=_FENCE)

    await _stream("go", "sS")

    thread = "a2a:sS:goal-iter-4" if fresh else "a2a:sS"
    (tool,) = await _tool_messages(g, thread)
    _assert_blocked(tool)


@pytest.fixture
def _overflow_retry(monkeypatch):
    _RAISED.clear()

    async def _compacted(exc, tid, sid):
        return "maximum context length" in str(exc)

    monkeypatch.setattr(chat_mod, "_overflow_compacted", _compacted)
    yield
    _RAISED.clear()


@pytest.mark.asyncio
async def test_e2e_sync_overflow_retry_keeps_the_folded_narrowing(env, monkeypatch, _overflow_retry):
    g = _graph(
        monkeypatch,
        _OverflowFirst(messages=iter([_call("current_time", "c1"), AIMessage(content="done")]), disable_streaming=True),
    )
    steering.enqueue("sO", "relayed text", msg_id="m1", fence=_FENCE)

    await chat_mod.chat("hello", "sO")

    assert _RAISED == [True]
    (tool,) = await _tool_messages(g, "a2a:sO")
    _assert_blocked(tool)


@pytest.mark.asyncio
async def test_e2e_stream_overflow_retry_keeps_the_folded_narrowing(env, monkeypatch, _overflow_retry):
    g = _graph(
        monkeypatch,
        _OverflowFirst(messages=iter([_call("current_time", "c1"), AIMessage(content="done")]), disable_streaming=True),
    )
    steering.enqueue("sQ", "relayed text", msg_id="m1", fence=_FENCE)

    await _stream("hello", "sQ")

    assert _RAISED == [True]
    (tool,) = await _tool_messages(g, "a2a:sQ")
    _assert_blocked(tool)


@pytest.mark.asyncio
async def test_carried_fence_intersects_and_fails_closed(monkeypatch):
    import runtime.state as rs

    class _G:
        def __init__(self, values=None, boom=False):
            self.values, self.boom = values, boom

        async def aget_state(self, config):
            if self.boom:
                raise RuntimeError("checkpointer down")
            return SimpleNamespace(values=self.values or {})

    cfg = {"configurable": {"thread_id": "t"}}
    monkeypatch.setattr(rs.STATE, "graph", _G({"subagent_fence": ["a", "b"]}), raising=False)
    assert await turn_stream._carried_fence(cfg, []) == ["a", "b"]
    assert await turn_stream._carried_fence(cfg, ["b", "c"]) == ["b"]
    monkeypatch.setattr(rs.STATE, "graph", _G({}), raising=False)
    assert await turn_stream._carried_fence(cfg, []) == []
    assert await turn_stream._carried_fence(cfg, ["x"]) == ["x"]
    monkeypatch.setattr(rs.STATE, "graph", _G(boom=True), raising=False)
    assert await turn_stream._carried_fence(cfg, []) == [FENCE_DENY_ALL]


# ── the calling turn's fence, as tool bodies see it ─────────────────────────


def test_fence_scope_nests_by_intersection():
    assert current_fence() == []
    with fence_scope(["a", "b"]):
        assert current_fence() == ["a", "b"]
        with fence_scope([]):  # an unfenced nested turn adds no restriction
            assert current_fence() == ["a", "b"]
        with fence_scope(["b", "c"]):
            assert current_fence() == ["b"]
    assert current_fence() == []
    assert normalize_fence("read_file") == [FENCE_DENY_ALL]  # unusable → closed, not open


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [["current_time"], None], ids=["fenced", "unfenced"])
async def test_e2e_tool_body_reads_the_calling_turns_fence(env, monkeypatch, fence):
    import tools.lg_tools as lg

    seen: list[list[str]] = []
    real_zone = lg.ZoneInfo

    def _spy(name):
        seen.append(current_fence())
        return real_zone(name)

    monkeypatch.setattr(lg, "ZoneInfo", _spy)
    _real_graph(monkeypatch, [_call("current_time", "c1"), AIMessage(content="done")])

    await _stream("time?", "sT", request_metadata={"subagent_fence": fence} if fence else None)

    assert seen == [fence or []]
    assert current_fence() == []  # the scope closed with the call


# ── background jobs: the nudge carries the spawning turn's fence ─────────────


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        raise ValueError("no body")


class _Client:
    posts: list[dict] = []

    def __init__(self, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, headers=None, json=None):
        _Client.posts.append(json)
        return _Resp()


def _manager(tmp_path):
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    return BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=BackgroundStore(str(tmp_path / "jobs.db")),
        api_key="k",
        bearer_token="b",
    )


@pytest.fixture
def _http(monkeypatch):
    import httpx

    _Client.posts = []
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return _Client


async def _settle(mgr):
    import asyncio

    for _ in range(100):
        if not mgr._fire_tasks:
            return
        await asyncio.sleep(0.01)


def _meta(post):
    return post["params"]["message"]["metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [_FENCE, []], ids=["fenced-origin", "unfenced-origin"])
async def test_background_nudge_carries_the_spawning_turns_fence(tmp_path, _http, fence):
    mgr = _manager(tmp_path)
    with fence_scope(fence):
        jid = await mgr.spawn(origin_session="o1", subagent_type="custom-role", description="d", prompt="p")
        wid = await mgr.spawn_work(origin_session="o1", kind="ingest", description="w", work=_noop)
    await _settle(mgr)
    assert mgr.store.get(jid).origin_fence == fence
    assert mgr.store.get(wid).origin_fence == fence

    _http.posts.clear()
    await mgr.resume_origin(mgr.store.get(jid))
    await mgr.resume_origin(mgr.store.get(wid))

    for post in _http.posts:
        assert post["params"]["message"]["contextId"] == "o1"
        assert _meta(post).get("subagent_fence") == (fence or None)


async def _noop():
    return "ok"


@pytest.mark.asyncio
async def test_background_batch_nudge_carries_the_members_fence(tmp_path, _http):
    mgr = _manager(tmp_path)
    with fence_scope(_FENCE):
        for i in range(2):
            await mgr.spawn(origin_session="o1", subagent_type="custom-role", description=f"d{i}", prompt="p", batch_id="b1")
    await mgr.spawn(origin_session="o2", subagent_type="custom-role", description="x", prompt="p", batch_id="b2")
    await _settle(mgr)
    _http.posts.clear()

    await mgr.resume_origin_batch("b1", "o1")
    await mgr.resume_origin_batch("b2", "o2")

    fenced, unfenced = _http.posts
    assert _meta(fenced)["subagent_fence"] == _FENCE
    assert "subagent_fence" not in _meta(unfenced)


@pytest.mark.asyncio
async def test_background_job_runs_under_its_subagent_and_origin_fence(tmp_path, _http):
    from graph.subagents.config import SUBAGENT_REGISTRY

    tools = list(SUBAGENT_REGISTRY["researcher"].tools)
    mgr = _manager(tmp_path)
    with fence_scope([tools[0], "not_a_researcher_tool"]):
        await mgr.spawn(origin_session="o1", subagent_type="researcher", description="d", prompt="p")
    await _settle(mgr)

    (fire,) = _http.posts
    assert _meta(fire)["subagent_fence"] == [tools[0]]  # narrowest wins


def test_background_store_reads_an_unreadable_fence_closed(tmp_path):
    import sqlite3

    from background.store import BackgroundStore

    store = BackgroundStore(str(tmp_path / "jobs.db"))
    jid = store.create(agent_name="a", origin_session="o", subagent_type="t", description="d", prompt="p")
    assert store.get(jid).origin_fence == []
    with sqlite3.connect(str(tmp_path / "jobs.db")) as db:
        db.execute("UPDATE background_jobs SET origin_fence = 'garbage' WHERE id = ?", (jid,))
    assert store.get(jid).origin_fence == [FENCE_DENY_ALL]


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [_FENCE, []], ids=["fenced-origin", "unfenced-origin"])
async def test_e2e_background_nudge_turn_runs_under_the_origin_fence(env, monkeypatch, tmp_path, _http, fence):
    """The nudge's metadata drives the origin-session turn: fenced origin → the briefing
    turn is fenced; unfenced origin → it runs unfenced (even on a thread a fenced turn
    left fenced)."""
    mgr = _manager(tmp_path)
    with fence_scope(fence):
        jid = await mgr.spawn(origin_session="oE", subagent_type="custom-role", description="d", prompt="p")
    await _settle(mgr)
    _http.posts.clear()
    await mgr.resume_origin(mgr.store.get(jid))
    (nudge,) = _http.posts
    msg = nudge["params"]["message"]

    g = _real_graph(monkeypatch, [AIMessage(content="ok"), _call("current_time", "c1"), AIMessage(content="brief")])
    # The thread's last turn is the OPPOSITE of the origin's, so neither case can pass by
    # inheriting it: an unfenced origin follows a fenced turn, a fenced one a plain turn.
    await _stream("earlier", "oE", request_metadata=None if fence else {"subagent_fence": ["ask_human"]})
    await _stream(msg["parts"][0]["text"], "oE", request_metadata=msg["metadata"])

    (tool,) = await _tool_messages(g, "a2a:oE")
    if fence:
        _assert_blocked(tool)
    else:
        _assert_ran(tool)


# ── scheduler: a fire carries the creating turn's fence ─────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [_FENCE, []], ids=["fenced", "unfenced"])
async def test_scheduler_fire_carries_the_creating_turns_fence(tmp_path, monkeypatch, fence):
    import httpx

    from scheduler.local import LocalScheduler

    class _R:
        status_code = 200
        text = ""

        def json(self):
            return {"result": {"status": {"state": "TASK_STATE_COMPLETED"}}}

    posts: list[dict] = []

    class _C(_Client):
        async def post(self, url, headers=None, json=None):
            posts.append(json)
            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", _C)
    s = LocalScheduler(agent_name="t", invoke_url="http://127.0.0.1:7870", api_key="k", bearer_token="b", db_dir=tmp_path)
    with fence_scope(fence):
        job = s.add_job("resume", "2099-01-01T00:00:00+00:00", job_id="wait:o1", context_id="o1")
    assert job.fence == fence
    (stored,) = [j for j in s.list_jobs() if j.id == "wait:o1"]
    assert stored.fence == fence

    assert await s._fire(stored) is True

    (post,) = posts
    assert post["params"]["message"]["contextId"] == "o1"
    assert _meta(post).get("subagent_fence") == (fence or None)


# ── watches + goals: reactions fire under the creating turn's fence ──────────


class _Sched:
    def __init__(self):
        self.added: list[dict] = []

    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        self.added.append({"prompt": prompt, "context_id": context_id, "fence": current_fence()})
        return SimpleNamespace(id=job_id or "j")

    def cancel_job(self, job_id):
        return True


@pytest.mark.platform_sensitive  # the command verifier spawns a process
@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [_FENCE, []], ids=["fenced", "unfenced"])
async def test_watch_reaction_runs_under_the_creating_turns_fence(tmp_path, monkeypatch, fence):
    from graph.watches.controller import WatchController
    from graph.watches.store import WatchStore
    from runtime.state import STATE

    sched = _Sched()
    monkeypatch.setattr(STATE, "scheduler", sched)
    c = WatchController(LangGraphConfig(), WatchStore(tmp_path))
    with fence_scope(fence):
        _ok, _m, w = c.create(
            condition="deploy done",
            verifier={"type": "command", "command": "exit 0"},
            run_prompt="Run the smoke test.",
            run_session="sess-7",
            trusted=True,
        )
    assert w.fence == fence

    assert await c.evaluate(w.id) == "met"  # fired from the watch loop — no turn in scope

    (job,) = sched.added
    assert job["context_id"] == "sess-7" and job["fence"] == fence


@pytest.mark.asyncio
async def test_a_fenced_watch_edit_narrows_the_watch(tmp_path):
    from graph.watches.controller import WatchController
    from graph.watches.store import WatchStore

    c = WatchController(LangGraphConfig(), WatchStore(tmp_path))
    _ok, _m, w = c.create(condition="c", verifier={"type": "plugin", "check": "p:v"}, run_session="s", run_prompt="x")
    assert w.fence == []
    with fence_scope(_FENCE):
        ok, _m, w2 = await c.update(w.id, run_prompt="y")
    assert ok and w2.fence == _FENCE
    ok, _m, w3 = await c.update(w.id, run_prompt="z")  # an unfenced edit doesn't widen it
    assert ok and w3.fence == _FENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [_FENCE, []], ids=["fenced", "unfenced"])
async def test_goal_hooks_run_under_the_setting_turns_fence(tmp_path, fence):
    from graph.goals.controller import GoalController
    from graph.goals.hooks import set_goal_hooks
    from graph.goals.store import GoalStore
    from graph.goals.types import VerifyResult
    from graph.goals.verifiers import set_plugin_verifiers

    seen: list[list[str]] = []
    set_goal_hooks([{"plugin_id": "p", "on_achieved": lambda s: seen.append(current_fence()), "on_failed": None}])

    async def _met(spec, ctx):
        return VerifyResult(True, "ok", "")

    set_plugin_verifiers({"p:always": _met})
    try:
        c = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
        with fence_scope(fence):
            c.set_goal_safe("s", "cond", {"type": "plugin", "check": "p:always"})
        assert c.active_goal("s").fence == fence
        await c.evaluate("s", last_text="done")  # evaluated by the driver, outside any tool
        assert seen == [fence]
    finally:
        set_goal_hooks([])
        set_plugin_verifiers({})
