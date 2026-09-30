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

# Imports the goal verifiers (platform-branching) and spawns a verifier process.
pytestmark = pytest.mark.platform_sensitive

chat_mod = importlib.import_module("server.chat")
turn_stream = importlib.import_module("server.turn_stream")

_FENCE = ["discord_read"]
_OVERFLOW = "Error code: 400 - This model's maximum context length is 128000 tokens."


@pytest.fixture(autouse=True)
def _clean_queues():
    steering._QUEUES.clear()
    yield
    steering._QUEUES.clear()


@pytest.fixture(autouse=True)
def _no_ledger(monkeypatch):
    """These tests spawn background jobs and run real delegations, and both record
    delegation edges into ``STATE.ledger_store``. An earlier test in the session can
    leave a real store there, so run with no ledger rather than leak edges into it."""
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "ledger_store", None, raising=False)


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
            await mgr.spawn(
                origin_session="o1", subagent_type="custom-role", description=f"d{i}", prompt="p", batch_id="b1"
            )
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
    s = LocalScheduler(
        agent_name="t", invoke_url="http://127.0.0.1:7870", api_key="k", bearer_token="b", db_dir=tmp_path
    )
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


# ── a goal a fenced turn set is pursued fenced — by every turn that drives it ─


async def _drive(driver, message, session_id):
    if driver == "stream":
        return await _stream(message, session_id)
    return await chat_mod.chat(message, session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
@pytest.mark.parametrize("fresh", [False, True], ids=["same-thread", "fresh-context"])
@pytest.mark.parametrize("goal_fence", [_FENCE, []], ids=["fenced-goal", "unfenced-goal"])
async def test_e2e_plain_turn_driving_a_goal_runs_under_the_goals_fence(env, monkeypatch, driver, fresh, goal_fence):
    """A fenced turn set the goal (``GoalState.fence``); the session's next PLAIN turn
    drives it — the goal-kickoff pass and the continuation both run under that fence."""
    goals = FakeGoals([("continue", "again", "iterate"), ("done", "met")], iteration=3, fresh=fresh)
    goals.state.fence = goal_fence
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = _real_graph(
        monkeypatch,
        [
            _call("current_time", "c0"),
            AIMessage(content="one"),
            _call("current_time", "c1"),
            AIMessage(content="two"),
        ],
    )
    sid = f"sG{driver}{int(fresh)}{len(goal_fence)}"

    await _drive(driver, "hi", sid)

    first = await _tool_messages(g, f"a2a:{sid}")
    cont = await _tool_messages(g, f"a2a:{sid}:goal-iter-4") if fresh else first[1:]
    tools = [first[0], *cont]
    assert [t.tool_call_id for t in tools] == ["c0", "c1"]
    for tool in tools:
        (_assert_blocked if goal_fence else _assert_ran)(tool)


@pytest.mark.asyncio
async def test_a_goal_set_under_a_fence_records_it_and_null_reads_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {"p:x": object()})
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore
    from graph.goals.types import GoalState
    from graph.watches.types import Watch

    c = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
    with fence_scope(_FENCE):
        c.set_goal_safe("s", "cond", {"type": "plugin", "check": "p:x"})
    assert c.active_goal("s").fence == _FENCE
    # A pre-fence file (no key) is unfenced; a present-but-unusable fence is deny-all.
    assert GoalState.from_dict({"session_id": "s", "condition": "c"}).fence == []
    assert GoalState.from_dict({"session_id": "s", "condition": "c", "fence": None}).fence == [FENCE_DENY_ALL]
    assert Watch.from_dict({"id": "w", "condition": "c"}).fence == []
    assert Watch.from_dict({"id": "w", "condition": "c", "fence": None}).fence == [FENCE_DENY_ALL]


# ── synchronous `task()` subagents run under the parent turn's fence ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_fence", "expect_ran"),
    [(["task"], False), (["task", "current_time"], True), (None, True)],
    ids=["disjoint-deny-all", "overlap", "unfenced-parent"],
)
async def test_e2e_sync_subagent_runs_under_the_parent_fence(env, monkeypatch, parent_fence, expect_ran):
    import runtime.state as rs
    import tools.lg_tools as lg
    from langgraph.checkpoint.memory import MemorySaver

    ran: list[str] = []
    real_zone = lg.ZoneInfo
    monkeypatch.setattr(lg, "ZoneInfo", lambda name: (ran.append(name), real_zone(name))[1])
    fake = _ToolFake(
        messages=iter(
            [
                _call("task", "t1", {"description": "d", "prompt": "what time is it", "subagent_type": "researcher"}),
                _call("current_time", "s1"),  # the subagent's out-of-(parent)-fence call
                AIMessage(content="sub done"),
                AIMessage(content="done"),
            ]
        ),
        disable_streaming=True,
    )
    import graph.agent as agent_mod

    # The subagent's model is built at delegation time — keep the fake in place for the turn.
    monkeypatch.setattr(agent_mod, "create_llm", lambda *a, **k: fake)
    g = agent_mod.create_agent_graph(LangGraphConfig(), checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)

    await _stream("delegate", "sSub", request_metadata={"subagent_fence": parent_fence} if parent_fence else None)

    (task_msg,) = await _tool_messages(g, "a2a:sSub")  # the subagent's calls stay in the sub-graph
    assert task_msg.tool_call_id == "t1" and task_msg.status == "success"
    assert ran == (["UTC"] if expect_ran else [])


# ── minors: every reaction runs in the creating turn's scope ─────────────────


@pytest.mark.asyncio
async def test_watch_hooks_and_bus_events_run_in_the_watch_fence(tmp_path, monkeypatch):
    from graph.plugins.host import HOST
    from graph.watches.controller import WatchController
    from graph.watches.hooks import set_watch_hooks
    from graph.watches.store import WatchStore

    seen: list[tuple[str, list[str]]] = []
    set_watch_hooks([{"on_met": lambda w: seen.append(("hook", current_fence()))}])
    monkeypatch.setattr(HOST, "publish", lambda topic, data: seen.append((topic, current_fence())))
    try:
        c = WatchController(LangGraphConfig(), WatchStore(tmp_path))
        with fence_scope(_FENCE):
            _ok, _m, w = c.create(condition="c", verifier={"type": "plugin", "check": "p:v"})
        await c._react(w, "tripped")
        await c._finish(w, "expired", "late")
    finally:
        set_watch_hooks([])
    reactions = [(t, f) for t, f in seen if t in ("hook", "watch.met", "watch.expired")]
    assert reactions == [("hook", _FENCE), ("watch.met", _FENCE), ("watch.expired", _FENCE)]


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [_FENCE, []], ids=["fenced", "unfenced"])
async def test_goal_bus_event_in_scope_and_review_skipped_when_fenced(tmp_path, monkeypatch, fence):
    import graph.self_improvement as si
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore
    from graph.goals.types import VerifyResult
    from graph.goals.verifiers import set_plugin_verifiers
    from graph.plugins.host import HOST

    published: list[list[str]] = []
    reviews: list[str] = []
    monkeypatch.setattr(
        HOST, "publish", lambda topic, data: published.append(current_fence()) if topic == "goal.achieved" else None
    )
    monkeypatch.setattr(si, "schedule_review", lambda config, scheduler, state, **kw: reviews.append(state.session_id))

    async def _met(spec, ctx):
        return VerifyResult(True, "ok", "")

    set_plugin_verifiers({"p:always": _met})
    try:
        c = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
        with fence_scope(fence):
            c.set_goal_safe("s", "cond", {"type": "plugin", "check": "p:always"})
        await c.evaluate("s", last_text="done")
    finally:
        set_plugin_verifiers({})
    assert published == [fence]
    assert reviews == ([] if fence else ["s"])  # no /self-improve review for a fenced goal


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [_FENCE, None], ids=["fenced", "unfenced"])
async def test_lifecycle_reaction_runs_in_the_triggering_turns_fence(env, monkeypatch, fence):
    import asyncio

    import graph.lifecycle as lc

    seen: list[list[str]] = []

    async def _fire(event, payload):
        seen.append(current_fence())

    monkeypatch.setattr(lc, "fire", _fire)
    monkeypatch.setattr(lc, "should_emit_active", lambda now, last: (True, 0.0, "idle"))
    env.install(streams=[[text("r1", "ok")]])

    await _stream("hi", "sL", request_metadata={"subagent_fence": fence} if fence else None)
    await asyncio.sleep(0)

    assert seen == [fence or []]


class _GoalSetMidTurn(FakeGoals):
    """No goal when the turn starts; by the time the initial pass ends one exists that a
    fenced turn set meanwhile (a concurrent fenced turn, a hook) — with ``fence``."""

    def __init__(self, fence, **kw):
        super().__init__([("continue", "again", "iterate"), ("done", "met")], **kw)
        self.state.fence = fence
        self._checks = 0

    def active_goal(self, session_id):
        self._checks += 1
        return self.state if self._checks > 1 else None


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
async def test_e2e_continuation_of_a_goal_set_mid_turn_runs_under_its_fence(env, monkeypatch, driver):
    monkeypatch.setattr(env.state, "goal_controller", _GoalSetMidTurn(_FENCE, iteration=3), raising=False)
    g = _real_graph(monkeypatch, [AIMessage(content="one"), _call("current_time", "c1"), AIMessage(content="two")])
    sid = f"sM{driver}"

    await _drive(driver, "hi", sid)

    (tool,) = await _tool_messages(g, f"a2a:{sid}")
    _assert_blocked(tool)


# ── a session whose active goal is fenced: its turns get the goal's pre-turn gating ─
#
# A plain (unfenced) turn in a session whose ACTIVE goal a fenced turn set is goal-driven
# under that goal's fence, so the pre-turn chain gates it exactly as it gates a fenced
# caller: no short-circuit runs (the text reaches the fenced goal-driven turn verbatim)
# and an ACP runtime refuses it. `/goal` itself still runs, so the operator can always
# check, replace or clear the goal.

chat_acp = importlib.import_module("server.chat_acp")
chat_commands = importlib.import_module("server.chat_commands")
chat_dispatch = importlib.import_module("server.chat_dispatch")


@pytest.fixture
def _shortcuts(monkeypatch):
    """`/digest` is a workflow and `/synthesizer` a subagent; running either is recorded."""
    ran: list[str] = []

    def _wf(message):
        return ("digest", {}) if message.startswith("/digest") else None

    def _sub(message):
        return ("synthesizer", message.split(" ", 1)[1]) if message.startswith("/synthesizer ") else None

    async def _run_wf(name, inputs, on_step=None):
        ran.append(f"workflow:{name}")
        return "workflow output"

    async def _run_sub(sub_type, prompt, **kw):
        ran.append(f"subagent:{sub_type}")
        return "subagent output"

    monkeypatch.setattr(chat_commands, "_parse_workflow_command", _wf)
    monkeypatch.setattr(chat_commands, "_parse_subagent_command", _sub)
    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", _run_wf)
    monkeypatch.setattr(chat_commands, "_run_parsed_subagent", _run_sub)
    return ran


@pytest.fixture
def _acp_rt(monkeypatch):
    """An ACP runtime; the message each driver hands it is recorded instead of run."""
    import runtime.acp_runtime as acp_runtime

    ran: list[str] = []

    async def _acquire(tid):
        return object()

    async def _release(tid):
        return None

    async def _drive(rt, message):
        ran.append(message)
        yield ("done", "ran-on-acp")

    async def _collected(session_id, message):
        ran.append(message)
        return [{"role": "assistant", "content": "ran-on-acp"}]

    monkeypatch.setattr(acp_runtime, "is_acp_runtime", lambda cfg: True)
    monkeypatch.setattr(chat_acp, "_acp_acquire", _acquire)
    monkeypatch.setattr(chat_acp, "_acp_release", _release)
    monkeypatch.setattr(chat_acp, "_acp_drive_turn", _drive)
    monkeypatch.setattr(chat_acp, "_acp_turn_collected", _collected)
    return ran


def _goal(env, monkeypatch, fence):
    goals = FakeGoals(iteration=3)  # past the kickoff: the turn's text reaches the graph as-is
    goals.state.fence = fence
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    return goals


async def _reply(driver, message, session_id):
    """The turn's final answer text."""
    if driver == "stream":
        frames = await _stream(message, session_id)
        assert frames[-1][0] == "done", frames
        return frames[-1][1]
    (out,) = await chat_mod.chat(message, session_id)
    return out["content"]


def _script(env, driver, answer="goal answer"):
    if driver == "stream":
        return env.install(streams=[[text("r1", answer)]])
    from tests._turn_driver_fakes import turn_result

    return env.install(invokes=[turn_result(AIMessage(content=answer))])


def _graph_inputs(g, driver):
    return [inp for inp, _ in (g.stream_calls if driver == "stream" else g.invoke_calls)]


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
@pytest.mark.parametrize("message", ["/digest now", "/synthesizer do it", "/foobar do it"])
async def test_fenced_goal_turn_runs_no_short_circuit(env, monkeypatch, _shortcuts, driver, message):
    """A workflow, a subagent, an unknown `/command`: none runs — the text goes to the
    goal-driven turn verbatim, under the goal's fence."""
    _goal(env, monkeypatch, _FENCE)
    g = _script(env, driver)

    answer = await _reply(driver, message, f"sGF{driver}")

    assert _shortcuts == []
    assert answer == "goal answer"
    (graph_input,) = _graph_inputs(g, driver)
    assert graph_input["messages"][-1].content == message
    assert _fence_of(graph_input) == _FENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
@pytest.mark.parametrize(
    ("message", "ran"),
    [("/digest now", ["workflow:digest"]), ("/synthesizer do it", ["subagent:synthesizer"])],
)
async def test_unfenced_goal_turn_still_short_circuits(env, monkeypatch, _shortcuts, driver, message, ran):
    _goal(env, monkeypatch, [])
    env.install()  # no graph call

    answer = await _reply(driver, message, f"sGU{driver}")

    assert _shortcuts == ran
    assert answer.endswith("output")


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
async def test_fenced_goal_turn_is_refused_on_an_acp_runtime(env, monkeypatch, _acp_rt, driver):
    _goal(env, monkeypatch, _FENCE)
    env.install()

    answer = await _reply(driver, "keep going", f"sGA{driver}")

    assert answer == chat_dispatch._GOAL_FENCED_ACP_REFUSAL
    assert _acp_rt == []


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
async def test_unfenced_goal_turn_still_runs_on_an_acp_runtime(env, monkeypatch, _acp_rt, driver):
    _goal(env, monkeypatch, [])
    env.install()

    assert await _reply(driver, "keep going", f"sGB{driver}") == "ran-on-acp"
    assert _acp_rt == ["keep going"]


@pytest.fixture
def _real_goals(env, monkeypatch, tmp_path):
    """The real controller, holding an active goal a fenced turn set on session ``s``."""
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore

    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {"p:x": object()})
    c = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
    with fence_scope(_FENCE):
        ok, _msg = c.set_goal_safe("s", "cond", {"type": "plugin", "check": "p:x"})
    assert ok and c.active_goal("s").fence == _FENCE
    monkeypatch.setattr(env.state, "goal_controller", c, raising=False)
    return c


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
async def test_fenced_goal_turn_still_runs_goal_status_and_clear(env, _real_goals, _acp_rt, driver):
    """`/goal` is never gated — not even on an ACP runtime, where every other turn in the
    session is refused while the fenced goal is active. After `/goal clear` the session
    is back to normal: the next turn runs there."""
    env.install()

    assert "cond" in await _reply(driver, "/goal", "s")
    assert await _reply(driver, "/goal clear", "s") == "Goal cleared."
    assert _real_goals.active_goal("s") is None
    assert await _reply(driver, "hello", "s") == "ran-on-acp"
    assert _acp_rt == ["hello"]


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
async def test_operator_can_replace_a_fenced_goal(env, _real_goals, _acp_rt, driver):
    """`/goal <new>` replaces the fenced goal with the operator's own (unfenced) one, and
    the turn it kicks off is gated by the NEW goal, not the one it replaced."""
    env.install()

    await _reply(driver, "/goal ship the release notes", "s")

    goal = _real_goals.active_goal("s")
    assert goal.condition == "ship the release notes" and goal.fence == []
    assert _acp_rt == ["/goal ship the release notes"]


@pytest.mark.asyncio
async def test_a_fenced_caller_still_cannot_change_the_goal(env, monkeypatch, _real_goals):
    """The `/goal` exemption is for the session's own (unfenced) turns: a fenced caller's
    `/goal clear` stays text for its fenced turn, and the goal survives."""

    async def _not_judged(session_id, **kw):
        return None  # one pass, no continuation

    monkeypatch.setattr(_real_goals, "evaluate", _not_judged)
    g = _script(env, "stream", "noted")

    frames = await _stream("/goal clear", "s", request_metadata={"subagent_fence": _FENCE})

    assert frames[-1] == ("done", "noted")
    assert _real_goals.active_goal("s") is not None
    ((graph_input, _),) = g.stream_calls
    assert _fence_of(graph_input) == _FENCE


@pytest.fixture
def _more_shortcuts(monkeypatch):
    """An `@proto` delegate, a plugin `/issue` command and a `/triage` skill; running
    (or, for the skill, rewriting) any of them is recorded."""
    chat_rooms = importlib.import_module("server.chat_rooms")
    ran: list[str] = []

    def _at(message):
        return (["proto"], message.split(" ", 1)[1]) if message.startswith("@proto ") else None

    async def _exchange(message, session_id, request_metadata):
        if _at(message) is None:
            return None, None  # not addressed: fall through, as the real exchange does
        ran.append("delegate:proto")
        return "delegate reply", [{"author": "proto", "reply": "delegate reply", "ok": True}]

    async def _plugin(name, rest, session_id):
        if name == "issue":
            ran.append("plugin:issue")
            return "plugin reply"
        return None

    def _skill(message):
        return ({"name": "triage", "prompt_template": "P"}, "x") if message.startswith("/triage") else None

    def _directive(skill, args):
        ran.append("skill:triage")
        return "SKILL DIRECTIVE"

    monkeypatch.setattr(chat_rooms, "_parse_at_delegates", _at)
    monkeypatch.setattr(chat_rooms, "_at_delegate_exchange", _exchange)
    monkeypatch.setattr(chat_dispatch, "_run_plugin_chat_command", _plugin)
    monkeypatch.setattr(chat_commands, "_parse_skill_command", _skill)
    monkeypatch.setattr(chat_commands, "_skill_directive", _directive)
    return ran


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
@pytest.mark.parametrize("message", ["@proto hi there", "/issue file it", "/triage x", "/lifecycle"])
async def test_fenced_goal_turn_gates_every_other_short_circuit(env, monkeypatch, _more_shortcuts, driver, message):
    """An @-mention, a plugin command, a skill rewrite, /lifecycle: none runs — the text
    reaches the goal-driven turn verbatim, under the goal's fence."""
    _goal(env, monkeypatch, _FENCE)
    g = _script(env, driver)

    answer = await _reply(driver, message, f"sGM{driver}")

    assert _more_shortcuts == []
    assert answer == "goal answer"
    (graph_input,) = _graph_inputs(g, driver)
    assert graph_input["messages"][-1].content == message
    assert _fence_of(graph_input) == _FENCE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "ran"),
    [("@proto hi there", ["delegate:proto"]), ("/issue file it", ["plugin:issue"]), ("/triage x", ["skill:triage"])],
)
async def test_unfenced_goal_turn_still_runs_the_other_short_circuits(env, monkeypatch, _more_shortcuts, message, ran):
    _goal(env, monkeypatch, [])
    _script(env, "stream")

    await _stream(message, "sGN")

    assert _more_shortcuts == ran


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "noticed"),
    [("/synthesizer do it", True), ("@proto hi", True), ("/typo", True), ("keep going", False), ("/usr/bin is odd", False)],
)
async def test_fenced_goal_turn_says_commands_are_paused(env, monkeypatch, _shortcuts, message, noticed):
    """A command the goal's scope keeps from running is announced, not silently turned
    into goal text; plain text gets no notice."""
    _goal(env, monkeypatch, _FENCE)
    _script(env, "stream")

    frames = await _stream(message, "sGP")

    assert (("tool_start", chat_dispatch._GOAL_FENCED_COMMANDS_PAUSED) in frames) is noticed
    assert _shortcuts == []


def test_goal_status_says_the_goal_runs_with_a_restricted_tool_scope():
    from graph.goals.types import GoalState

    scoped = GoalState(session_id="s", condition="c", fence=_FENCE).status_line()
    assert "restricted tool scope" in scoped and "discord_read" not in scoped
    assert "restricted tool scope" not in GoalState(session_id="s", condition="c").status_line()


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
async def test_fenced_goal_session_answers_a_goal_parse_error(env, _real_goals, _acp_rt, driver):
    """A malformed `/goal` is still goal control — answered, not refused or run."""
    env.install()

    answer = await _reply(driver, '/goal {"condition": ', "s")

    assert answer.startswith("Could not parse goal")
    assert _acp_rt == [] and _real_goals.active_goal("s").fence == _FENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
async def test_a_deny_all_goal_gates_the_pre_turn_chain(env, monkeypatch, tmp_path, _shortcuts, _acp_rt, driver):
    """A stored goal whose fence is unusable (``null``) reads as deny-all — gated, and
    refused on an ACP runtime, never treated as unfenced."""
    import json

    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore

    (tmp_path / "s.json").write_text(
        json.dumps({"session_id": "s", "condition": "c", "verifier": {"type": "llm"}, "status": "active", "fence": None})
    )
    c = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
    assert c.active_goal("s").fence == [FENCE_DENY_ALL]
    monkeypatch.setattr(env.state, "goal_controller", c, raising=False)
    env.install()

    assert await _reply(driver, "/synthesizer do it", "s") == chat_dispatch._GOAL_FENCED_ACP_REFUSAL
    assert _shortcuts == [] and _acp_rt == []


@pytest.mark.asyncio
async def test_a_fenced_caller_still_cannot_change_the_goal_sync(env, monkeypatch, _real_goals):
    async def _not_judged(session_id, **kw):
        return None

    monkeypatch.setattr(_real_goals, "evaluate", _not_judged)
    from tests._turn_driver_fakes import turn_result

    g = env.install(invokes=[turn_result(AIMessage(content="noted"))])

    (out,) = await chat_mod.chat("/goal clear", "s", tool_fence=_FENCE, origin="plugin")

    assert out["content"] == "noted"
    assert _real_goals.active_goal("s") is not None
    ((graph_input, _),) = g.invoke_calls
    assert _fence_of(graph_input) == _FENCE


# ── the /btw side question runs under the session's goal fence ───────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goal_fence", "caller_fence", "expect"),
    [
        (_FENCE, None, _FENCE),
        ([], None, []),
        (_FENCE, ["current_time"], [FENCE_DENY_ALL]),
        ([], "not-a-list", [FENCE_DENY_ALL]),
        (["current_time", "discord_read"], ["current_time"], ["current_time"]),
    ],
)
async def test_aside_stamps_the_goal_and_caller_fence(env, monkeypatch, goal_fence, caller_fence, expect):
    session_ops = importlib.import_module("server.chat_session_ops")
    _goal(env, monkeypatch, goal_fence)
    seen: dict = {}

    class _G:
        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": []})

        async def ainvoke(self, graph_input, config=None):
            seen.update(graph_input)
            return {"messages": [AIMessage(content="aside answer")]}

    monkeypatch.setattr(env.state, "graph", _G(), raising=False)
    md = {"subagent_fence": caller_fence} if caller_fence is not None else None

    out = await session_ops.aside_session("sA", "what's up?", request_metadata=md)

    assert out["found"] is True
    assert seen["subagent_fence"] == expect


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_fence", [_FENCE, []], ids=["fenced-goal", "unfenced-goal"])
async def test_e2e_aside_in_a_fenced_goal_session_blocks_a_tool_outside_the_fence(env, monkeypatch, goal_fence):
    """The side question runs the REAL graph over the session's context: a tool outside
    the goal's fence is blocked there, as on every goal-driven pass."""
    session_ops = importlib.import_module("server.chat_session_ops")
    aside_op = importlib.import_module("graph.aside_op")
    monkeypatch.setattr(aside_op.secrets, "token_hex", lambda n: "x")
    _goal(env, monkeypatch, goal_fence)
    g = _real_graph(monkeypatch, [_call("current_time", "c0"), AIMessage(content="it's late")])
    sid = f"sAE{len(goal_fence)}"

    out = await session_ops.aside_session(sid, "what time is it?")

    assert out["found"] is True
    (tool,) = await _tool_messages(g, f"a2a:{sid}::aside-x")
    (_assert_blocked if goal_fence else _assert_ran)(tool)
