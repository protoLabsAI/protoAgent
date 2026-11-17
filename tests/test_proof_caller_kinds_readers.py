"""P1 proof K2: caller-kind runs for steering queues and derived readers.

The proof matrix (docs/design/conversations-in-work-proposal.md ~line 675) requires every
identity-sensitive scenario to run for *federation, fleet-service, paired-agent and
enrolled-console* callers — "no operator-tier shortcut" — so no pass rests on the single
``shared-operator-bearer`` (or hand-stuffed ``device``) identity the source tests use.

This file re-runs the queue-isolation and derived-reader scenarios named by that matrix for
each of the four caller kinds, with the principal **resolved through the real auth path**: an
authenticated request classified by ``A2AAuthMiddleware`` and the production
``tier_context_builder()`` ``ServerCallContext`` — never a hand-written ``state`` dict.

Two invariants are under test, both read at a real surface:

- **Queue isolation** — the segment's fixed audience (not the submitter's identity) decides
  what a model call or dispatch sees; one durable turn per message id. The peer-audience
  steer's ``principal`` is the resolved caller kind; the operator-side scheduling store is
  driven by a real human-operator context (it requires one — a private shell is only readable
  by a human operator).
- **Public-only derived readers** — gist, digest, harvest summary/archive/facts, tracing,
  prompt capture and fleet export carry no private canary for ANY caller kind, *including the
  human operator*: private content is dropped at projection, not gated at read time. The one
  reader with a real identity surface — the retained private archive — is recoverable only by
  the human-operator console (``enrolled-console``) and hidden from every peer.

Helpers are COPIED from the source test files; the source files are not modified, and nothing
under ``src/`` changes.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

# The real auth path pairs `device`/`agent` credentials through security.devices (OS-sensitive
# registry writes), so the Windows CI lane's PR subset must include these proofs.
pytestmark = pytest.mark.platform_sensitive

# ── the four caller kinds + the real auth path ───────────────────────────────────
#
# Proof-matrix kind -> (auth classification case, expected principal_kind, is_human_operator).
# paired-agent is an ``agent``-kind device; enrolled-console is a ``device``-kind device
# (ADR 0131 P1a). Only the shared bearer and ``device``-kind device read as the human operator;
# fleet-service, ``agent``-kind devices and federation are peers.
_CASE_FOR_KIND = {
    "federation": ("federation", "federation", False),
    "fleet-service": ("fleet", "fleet-service", False),
    "paired-agent": ("agent", "agent-device", False),
    "enrolled-console": ("device", "device", True),
}
CALLER_KINDS = list(_CASE_FOR_KIND)


class _Caller:
    """A caller kind whose principal was resolved through the real auth path."""

    def __init__(self, kind, ctx, principal_kind, principal, is_operator):
        self.kind = kind
        self.ctx = ctx  # the production ServerCallContext from tier_context_builder()
        self.principal_kind = principal_kind
        self.principal = principal  # the identity label threaded into scheduling acceptances
        self.is_operator = is_operator


def _resolve(case):
    """Resolve ONE caller's principal through the real auth path.

    An authenticated request is classified by ``A2AAuthMiddleware`` and the call context is
    built by the production ``tier_context_builder()`` — exactly the server's path. Returns the
    resulting ``ServerCallContext`` and the paired device id (``""`` for token credentials).
    Assumes the auth ladder + device registry are already configured (the ``auth_env`` fixture).
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from a2a_impl import auth
    from a2a_impl.stores import tier_context_builder
    from security import devices

    token = {"federation": "fed"}.get(case, case)
    device_id = ""
    if case in ("device", "agent"):
        code, _ = devices.start_pairing(kind=case)
        device, token = devices.claim_pairing(code, f"proof-{case}")  # (device.public() dict, token)
        device_id = str(device["id"])

    holder: dict = {}

    def inspect(request):
        holder["ctx"] = tier_context_builder().build(request)
        return JSONResponse(holder["ctx"].state["principal_kind"])

    app = Starlette(routes=[Route("/a2a", inspect)])
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        resp = client.get("/a2a", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return holder["ctx"], device_id


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """Configure the real auth ladder once and point the device registry at a tmp file, so each
    caller kind resolves through it. The device registry lives in a subdir — ``os.listdir``-based
    digest loaders skip non-``.json`` directory entries, so it never pollutes a memory dir that
    shares ``tmp_path``. The process-global auth tokens are snapshotted and restored."""
    from a2a_impl import auth
    from security import devices

    reg_dir = tmp_path / "caller_auth"
    reg_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(devices, "_registry_path", lambda: reg_dir / "devices.json")
    snap = (auth._BEARER[0], auth._FEDERATION[0], auth._FLEET[0], auth._API_KEY[0], auth._ALLOWED_ORIGINS[0])
    auth.configure(
        bearer_token="shared", federation_token="fed", fleet_token="fleet",
        api_key="legacy", allowed_origins_raw="",
    )
    yield
    (auth._BEARER[0], auth._FEDERATION[0], auth._FLEET[0], auth._API_KEY[0], auth._ALLOWED_ORIGINS[0]) = snap


@pytest.fixture(params=CALLER_KINDS)
def caller(request, auth_env):
    """The caller kind under test, resolved through the real auth path (not a shortcut)."""
    from a2a_impl.auth import is_human_operator

    kind = request.param
    case, expected_kind, is_operator = _CASE_FOR_KIND[kind]
    ctx, device_id = _resolve(case)
    # The real classifier produced the expected kind + tier — no hand-stuffed state.
    assert ctx.state["principal_kind"] == expected_kind
    assert is_human_operator(ctx.state["principal_kind"]) is is_operator
    principal = f"{expected_kind}:{device_id}" if device_id else expected_kind
    return _Caller(kind, ctx, expected_kind, principal, is_operator)


@pytest.fixture
def server_op(auth_env):
    """The server/operator context the private-partition machinery runs under, resolved via the
    real auth path (shared operator bearer). ``save_private`` and the scheduling store require a
    human operator; the CALLER KIND is the peer principal threaded into the acceptances/reads.
    Resolved in a sync fixture (setup time) so the TestClient portal never collides with an
    async test's running event loop."""
    ctx, _ = _resolve("shared")
    assert ctx.state["principal_kind"] == "shared-operator-bearer"
    return ctx


# ════════════════════════════════════════════════════════════════════════════════
# Queues — isolation holds for every caller kind
# ════════════════════════════════════════════════════════════════════════════════


class _ToolFake(GenericFakeChatModel):
    """Copied from tests/test_steering.py: a fake chat model that supports bind_tools so it
    drops into create_agent and replays preset AIMessages, including tool calls."""

    def bind_tools(self, tools, **kwargs):
        return self


async def _open_partition(tmp_path, name):
    """Copied from tests/test_steer_idle_starts_turn.py::_partition."""
    from a2a_impl import stores

    engine = stores.make_sqlite_engine(str(tmp_path / name))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    return engine, store


@pytest.mark.asyncio
@pytest.mark.parametrize("segment_audience", ["peer", "private"])
async def test_no_model_call_mixes_audiences_per_caller_kind(caller, segment_audience):
    """tests/test_steering.py::test_no_model_call_mixes_audiences_in_a_real_graph, re-run for
    each caller kind: the peer-audience steer is submitted by this resolved caller kind, and the
    segment's fixed audience — not the caller's identity — still decides which steer the next
    model call sees. No model call ever carries both audiences, whoever the peer is."""
    from unittest.mock import patch

    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import MemorySaver

    from graph import steering
    from graph.config import LangGraphConfig

    steering._reset()
    try:
        sid = f"seg-{caller.kind}-{segment_audience}"

        @tool
        def long_task() -> str:
            """A long step; both audiences steer the agent while it runs."""
            if segment_audience == "private":
                steering.enqueue(sid, "PEER-STEER", msg_id="p1")
                steering.enqueue(sid, "PRIVATE-STEER", msg_id="v1", audience=steering.AUDIENCE_PRIVATE)
            else:
                steering.enqueue(sid, "PRIVATE-STEER", msg_id="v1", audience=steering.AUDIENCE_PRIVATE)
                steering.enqueue(sid, "PEER-STEER", msg_id="p1")
            return "long_task step done"

        fake = _ToolFake(
            messages=iter(
                [
                    AIMessage(content="", tool_calls=[{"name": "long_task", "args": {}, "id": "c1", "type": "tool_call"}]),
                    AIMessage(content="done"),
                ]
            )
        )
        with patch("graph.agent.create_llm", lambda *a, **k: fake):
            from graph.agent import create_agent_graph

            graph = create_agent_graph(
                LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver(), extra_tools=[long_task]
            )
        result = await graph.ainvoke(
            {"messages": [HumanMessage("start")], "session_id": sid, "segment_audience": segment_audience},
            config={"configurable": {"thread_id": "t1"}},
        )
        folded = "\n".join(
            m.content for m in result["messages"] if isinstance(m, HumanMessage) and isinstance(m.content, str)
        )
        if segment_audience == "peer":
            assert "PEER-STEER" in folded and "PRIVATE-STEER" not in folded
            assert steering.pending_items(sid) == [{"id": "v1", "text": "PRIVATE-STEER", "audience": "private"}]
        else:
            assert "PRIVATE-STEER" in folded and "PEER-STEER" not in folded
            assert steering.pending_items(sid) == [{"id": "p1", "text": "PEER-STEER"}]
    finally:
        steering._reset()


@pytest.mark.asyncio
async def test_cross_audience_steer_queues_behind_running_segment_per_caller_kind(caller, server_op, tmp_path):
    """tests/test_steer_idle_starts_turn.py::test_a_cross_audience_steer_queues_behind_the_running_segment,
    re-run for each caller kind: a peer steer SUBMITTED BY THIS CALLER (its resolved principal)
    must not fold into the running private segment and queues behind it. Audience is fixed when
    the segment is claimed; next_dispatch never crosses audiences regardless of who submitted."""
    from a2a_impl import segment_schedule as ss

    engine, store = await _open_partition(tmp_path, "cross.db")
    try:
        op = server_op  # the operator-side scheduling store requires a human operator
        priv = await ss.accept_pending_turn(
            store, session_id="chat-q", audience=ss.AUDIENCE_PRIVATE, principal="shared-operator-bearer",
            dedupe="m1", input_ref="private-input:x", context=op,
        )
        gen = await ss.claim_segment(store, "chat-q", audience=ss.AUDIENCE_PRIVATE, seq=priv["seq"], context=op)
        await ss.set_turn_state(store, "chat-q", priv["seq"], ss.STATE_RUNNING, op)
        # The peer steer is this caller kind's, identified by its real-auth-resolved principal.
        await ss.accept_pending_turn(
            store, session_id="chat-q", audience=ss.AUDIENCE_PEER, principal=caller.principal, dedupe="m2", context=op,
        )
        assert await ss.try_fold(store, "chat-q", generation=gen, audience=ss.AUDIENCE_PEER, seq=2, context=op) is None
        assert await ss.next_dispatch(store, "chat-q", op) is None  # reserved by the private segment
        assert await ss.complete_segment(store, "chat-q", generation=gen, context=op) is True
        nxt = await ss.next_dispatch(store, "chat-q", op)
        assert nxt["dedupe"] == "m2" and nxt["audience"] == "peer"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_message_id_gives_one_pending_turn_per_caller_kind(caller, server_op, tmp_path):
    """tests/test_steer_idle_starts_turn.py::test_duplicate_message_id_gives_exactly_one_pending_turn,
    re-run for each caller kind: a retried message id from this caller reuses the one pending
    turn — one durable turn per message id, in acceptance order, whoever the submitter is."""
    from sqlalchemy import text as _sql

    from a2a_impl import segment_schedule as ss

    engine, store = await _open_partition(tmp_path, "dup.db")
    try:
        op = server_op
        first = await ss.accept_pending_turn(
            store, session_id="chat-d", audience=ss.AUDIENCE_PEER, principal=caller.principal, dedupe="m1", context=op,
        )
        again = await ss.accept_pending_turn(
            store, session_id="chat-d", audience=ss.AUDIENCE_PEER, principal=caller.principal, dedupe="m1", context=op,
        )
        assert again["seq"] == first["seq"]  # the retry reuses the one turn
        async with store.engine.connect() as conn:
            n = (await conn.execute(_sql("SELECT COUNT(*) FROM tasks WHERE public_visible=0"))).scalar()
        assert n == 1  # one record, not two
    finally:
        await engine.dispose()


# ════════════════════════════════════════════════════════════════════════════════
# Derived readers — public-only for every caller kind
# ════════════════════════════════════════════════════════════════════════════════


# ── gist (copied harness from tests/test_session_gist.py) ─────────────────────────


@pytest.fixture
def gist_harness(tmp_path, monkeypatch):
    """Copied from tests/test_session_gist.py::harness."""
    from graph import session_gist
    from graph.config import LangGraphConfig
    from graph.middleware import memory

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(memory, "_PERSISTENCE_DISABLED", False)
    monkeypatch.setattr(session_gist, "_pending", {})
    timers = []
    calls = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback = delay, callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            self.callback()

    def invoke(messages):
        calls.append(json.loads(messages[-1].content))
        return AIMessage(content="We are designing the idle session gist. Implementation is ready for review. " * 5)

    def model(config, role):
        assert role == "memory_gist"
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(session_gist, "Timer", Timer)
    monkeypatch.setattr(session_gist, "create_role_llm", model)
    config = LangGraphConfig()
    mw = memory.SessionSummaryMiddleware(config=config)
    state = {"session_id": "chat-1", "messages": []}

    def turn(pairs=1):
        mw.before_agent(state, None)
        for _ in range(pairs):
            state["messages"].extend([HumanMessage(content="Design this feature"), AIMessage(content="Working on it")])
        mw.after_agent(state, None)

    def card():
        return json.loads((tmp_path / memory.session_filename(state["session_id"])).read_text())

    return SimpleNamespace(mw=mw, state=state, timers=timers, calls=calls, turn=turn, card=card, config=config)


def test_private_source_survives_public_turn_per_caller_kind(caller, gist_harness, tmp_path, monkeypatch):
    """tests/test_session_gist.py::test_private_source_survives_public_turn_compaction_restart_and_stale_timer,
    re-run for each caller kind: the idle-session gist is a PUBLIC projection, so no private
    canary survives into it across a public turn, compaction, restart and a stale timer —
    regardless of which caller kind later consumes it (peer or the human operator)."""
    from langchain_core.messages import ToolMessage

    from graph import public_memory
    from graph.compaction_op import compact_thread
    from graph.middleware import memory
    from graph import session_gist
    from tests._public_memory_source import PublicMemorySource
    from tests.test_compaction_op import _cfg, _FakeGraph, _FakeHistory

    h = gist_harness
    h.state["messages"] = [HumanMessage(content="PRIVATE-CANARY")] * 4
    h.turn(2)
    stale = h.timers[-1]
    path = tmp_path / "chat-1.json"
    path.write_text(json.dumps({**h.card(), "gist": "PRIVATE-OLD-GIST"}))
    source = PublicMemorySource(tmp_path / "source.record")
    public = [HumanMessage(content="rocket planning"), AIMessage(content="public answer")] * 3
    source.save(public)
    monkeypatch.setattr(public_memory, "_source_reader", source)
    stale.fire()
    assert not h.calls
    h.state["messages"].extend([
        AIMessage(content="PRIVATE-REPLY", tool_calls=[{"name": "lookup", "args": {"x": "PRIVATE-ARGS"}, "id": "p"}]),
        ToolMessage(content="PRIVATE-TOOL", tool_call_id="p"),
    ])
    h.state["incognito"] = False
    h.turn()
    stale.fire()
    h.timers[-1].fire()
    assert h.calls[-1]["previous_gist"] == ""
    assert h.card()["message_count"] == len(public)
    assert h.card()["tool_calls"] == []
    assert h.card()["final_output"] == "public answer"

    async def summarize(*args):
        return "PRIVATE-COMPACTED"

    graph = _FakeGraph(h.state["messages"])
    result = asyncio.run(compact_thread(
        graph, object(), None, _cfg(2), "chat-1", "chat-1", history_store=_FakeHistory(), summarizer=summarize,
    ))
    assert not result["refused"]
    h.state["messages"] = graph.updates[-1][1]["messages"][1:]
    monkeypatch.setattr(public_memory, "_source_reader", PublicMemorySource(source.path))
    h.mw = memory.SessionSummaryMiddleware(config=h.config)
    h.mw.after_agent(h.state, None)
    # The public gist/digest a reader of any kind sees carries no private canary.
    assert "PRIVATE" not in json.dumps(h.card()) + json.dumps(h.calls)
    assert "rocket planning" in memory.load_digest(memory_dir=str(tmp_path)).block

    source.save(public * 2)
    h.mw.after_agent(h.state, None)
    before = h.card()

    def invoke(_):
        source.save(public * 3)
        return AIMessage(content="PRIVATE-STALE")

    monkeypatch.setattr(session_gist, "create_role_llm", lambda *_: SimpleNamespace(invoke=invoke))
    h.timers[-1].fire()
    assert h.card() == before
    assert memory.load_digest(memory_dir=str(tmp_path)).entries == []


# ── digest (copied helpers from tests/test_digest_gating.py) ──────────────────────


def _summary(session_id: str, topic: str, *, gist: str = "") -> dict:
    """Copied from tests/test_digest_gating.py."""
    summary = {
        "session_id": session_id,
        "trace_id": f"trace-{session_id}",
        "messages": [{"role": "user", "content": topic}],
        "final_output": f"done: {topic}",
        "timestamp": "2026-08-28T12:00:00+00:00",
    }
    if gist:
        summary["gist"] = gist
    return summary


def _write(memory_dir, session_id: str, topic: str, *, mtime=None, gist: str = "") -> None:
    """Copied from tests/test_digest_gating.py."""
    import os

    from graph.middleware.memory import session_filename

    path = memory_dir / session_filename(session_id)
    path.write_text(json.dumps(_summary(session_id, topic, gist=gist)), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _quiet_working_state(monkeypatch) -> None:
    monkeypatch.setattr("graph.projection.working_state_block", lambda state: "")


@pytest.mark.parametrize(
    "provenance",
    [None, {"audience": "mixed", "version": "1"}, {"audience": "public", "version": "old"}],
)
def test_public_projection_filters_prompts_per_caller_kind(caller, tmp_path, monkeypatch, provenance):
    """tests/test_digest_gating.py::test_public_projection_filters_open_closed_relevant_and_cached_prompts,
    re-run for each caller kind: the digest / composed peer prompt is a public projection, so a
    private canary is filtered from open, closed, relevant and cached prompts — whoever reads."""
    from datetime import datetime, timezone

    from graph import public_memory
    from graph.middleware import memory
    from graph.middleware.knowledge import KnowledgeMiddleware
    from graph.middleware.memory import load_digest
    from graph.session_search import search_session_summaries
    from tests._public_memory_source import PublicMemorySource

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(memory, "_PERSISTENCE_DISABLED", False)
    _quiet_working_state(monkeypatch)
    _write(tmp_path, "chat-1", "PRIVATE-CANARY", gist="PRIVATE-GIST")
    path = tmp_path / "chat-1.json"
    old = json.loads(path.read_text())
    if provenance is not None:
        old[public_memory.PROVENANCE] = provenance
    path.write_text(json.dumps(old))
    mw = KnowledgeMiddleware(None)
    mw.compose_context({"session_id": "peer", "messages": [HumanMessage(content="hello")]}, record=False)
    search_session_summaries("PRIVATE", memory_dir=str(tmp_path))
    source = PublicMemorySource(tmp_path / "source.record")
    source.save([HumanMessage(content="rocket launch"), AIMessage(content="ready")])
    monkeypatch.setattr(public_memory, "_source_reader", source)
    closed = tmp_path / "closed"
    closed.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    (closed / path.name).write_text(json.dumps({**old, "closed_at": now}))
    assert not memory.record_closed_session("chat-1", topic="PRIVATE", gist="PRIVATE", closed_at=now)
    assert search_session_summaries("PRIVATE", memory_dir=str(tmp_path)) == []
    assert memory.digest_entry(old)["topic"] == ""
    assert memory.format_session_summary(old) == ""
    assert not memory.record_closed_session("chat-1", topic="PRIVATE", gist="PRIVATE", closed_at=now, source_card=old,
                                            audience="public", source_version=1)
    for policy in ("newest", "relevant"):
        assert load_digest(policy, query="PRIVATE", memory_dir=str(tmp_path)).entries == []
    assert "PRIVATE" not in (mw.compose_context({"session_id": "peer", "messages": []}, record=False) or {}).get("context", "")
    memory._persist_session({"session_id": "chat-1", "incognito": False,
                             "messages": [HumanMessage(content="PRIVATE-COMPACTED"), AIMessage(content="PRIVATE-REPLY")]}, "trace")
    card = json.loads(path.read_text())
    assert "PRIVATE" not in json.dumps(card)
    for policy in ("newest", "relevant"):
        assert "rocket launch" in load_digest(policy, query="rocket", memory_dir=str(tmp_path)).block
    card.update(audience="public", source_version=1)
    assert memory.record_closed_session("chat-1", topic="ignored", gist="ignored", closed_at=now, source_card=card,
                                        audience="mixed", source_version=0)
    closed_card = json.loads((closed / path.name).read_text())
    assert closed_card["audience"] == "public" and closed_card["source_version"] == 1
    path.unlink()
    monkeypatch.setattr(public_memory, "_source_reader", PublicMemorySource(source.path))
    restarted = KnowledgeMiddleware(None)
    context = restarted.compose_context({"session_id": "peer", "messages": []}, record=False)
    assert "rocket launch" in context["context"] and "PRIVATE" not in context["context"]
    source.path.unlink()
    memory._persist_session({"session_id": "chat-1", "incognito": False,
                             "messages": [HumanMessage(content="PRIVATE-UNKNOWN")]}, "trace")
    assert not path.exists()
    assert load_digest(memory_dir=str(tmp_path)).entries == []
    assert "rocket launch" not in restarted.compose_context({"session_id": "peer", "messages": []}, record=False)["context"]


# ── harvest (copied helpers from tests/test_conversation_harvest.py) ──────────────

_STEER_CANARY = "STEERSECRET acquire TargetCo before Friday"
_REPLY_CANARY = "PRIVATEREPLY understood, keeping the TargetCo plan quiet"
_ARCHIVE_CANARY = "LEGACYSECRET the TargetCo valuation is 4.2B"


@pytest.fixture
def history_store(tmp_path, monkeypatch):
    """Copied from tests/test_conversation_harvest.py::history_store."""
    from knowledge.history import HistoryStore
    from runtime.state import STATE

    store = HistoryStore(tmp_path / "kb.db")
    monkeypatch.setattr(STATE, "history_store", store)
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path / "memory"))
    return store


def _private(message):
    """Copied from tests/test_conversation_harvest.py."""
    from graph.conversation_audience import PRIVATE_AUDIENCE

    message.additional_kwargs["audience"] = PRIVATE_AUDIENCE
    return message


def _public(message):
    from graph.conversation_audience import PUBLIC_AUDIENCE

    message.additional_kwargs["audience"] = PUBLIC_AUDIENCE
    return message


def test_harvest_excludes_private_canaries_per_caller_kind(caller, tmp_path, history_store):
    """tests/test_conversation_harvest.py::test_harvest_excludes_private_canaries_from_every_derived_reader,
    re-run for each caller kind: every derived reader (summary, closed card, compaction archive,
    shared-history search, fact recall) is public-only for ALL kinds. The ONE identity surface —
    the retained private archive — is recoverable only by the human-operator console
    (``enrolled-console``, ``include_private``) and hidden from every peer."""
    import json as _json
    import os as _os
    from unittest.mock import AsyncMock

    from knowledge.history import HistoryStore
    from knowledge.memories import MemoryStore

    from graph.conversation_audience import PRIVATE_AUDIENCE, PUBLIC_AUDIENCE
    from graph.conversation_harvest import archive_payload, harvest_thread
    from graph.middleware.memory import memory_path, session_filename

    session_id = "chat-9"
    history_store.add(
        f"[Conversation archive] {_ARCHIVE_CANARY}",
        kind="archive", session_id=session_id, namespace=f"chat-archive:{session_id}", audience=PRIVATE_AUDIENCE,
    )
    history_store.add(
        "[Conversation archive] Earlier we compared teal and slate palettes.",
        kind="archive", session_id=session_id, namespace=f"chat-archive:{session_id}", audience=PUBLIC_AUDIENCE,
    )

    messages = [
        HumanMessage(content="help me pick a theme for the dashboard"),
        _private(HumanMessage(content=_STEER_CANARY)),
        _private(AIMessage(content=f"<output>{_REPLY_CANARY}</output>")),
        AIMessage(content="<output>A teal palette fits the dashboard.</output>"),
    ]
    saver = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={
        "channel_values": {
            "messages": messages, "session_id": session_id, "incognito": False, "operator_chat_turn": True,
        },
    })))

    seen_transcripts = []

    async def summarize(transcript, config):
        seen_transcripts.append(transcript)
        return "The operator wants a teal dashboard theme."

    async def facts(transcript, config):
        raise AssertionError("fact extraction must be disabled for mixed/unknown provenance")

    memory = MemoryStore(tmp_path / "agent.db")
    cid = asyncio.run(harvest_thread(
        "a2a:chat-9", checkpointer=saver, knowledge_store=None, history_store=history_store,
        memory_store=memory, config=SimpleNamespace(knowledge_facts=True),
        summarizer=summarize, fact_extractor=facts, raise_on_error=True,
    ))

    transcript = "".join(seen_transcripts)
    assert "teal and slate palettes" in transcript and "teal palette fits" in transcript
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in transcript

    summary = history_store.get(cid)
    assert summary.kind == "summary" and summary.audience == PUBLIC_AUDIENCE and summary.source_version == 1
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in summary.content

    for store in (history_store, HistoryStore(history_store.path)):
        for token in ("STEERSECRET", "PRIVATEREPLY", "LEGACYSECRET", "TargetCo"):
            assert store.search(token) == []
        # The archive read this CALLER KIND gets: a peer sees only the public archive; the
        # human-operator console recovers both (the private archive is retained, not a leak).
        caller_arcs = store.list_for_session(session_id, kinds=["archive"], include_private=caller.is_operator)
        if caller.is_operator:
            assert len(caller_arcs) == 2
        else:
            assert [a.content for a in caller_arcs] == ["[Conversation archive] Earlier we compared teal and slate palettes."]
            assert all(_ARCHIVE_CANARY not in a.content for a in caller_arcs)
        # The default (public) read never carries the private archive, for either kind.
        public_arcs = [a.content for a in store.list_for_session(session_id, kinds=["archive"])]
        assert public_arcs == ["[Conversation archive] Earlier we compared teal and slate palettes."]
        assert len(store.list_for_session(session_id, kinds=["archive"], include_private=True)) == 2

    closed_file = _os.path.join(memory_path(), "closed", session_filename(session_id))
    card = _json.loads(open(closed_file, encoding="utf-8").read())
    assert card["audience"] == PUBLIC_AUDIENCE and card["source_version"] == 1
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in _json.dumps(card)

    content, kwargs = archive_payload(messages, session_id=session_id, thread_id="a2a:chat-9", cause="auto-compaction")
    assert kwargs["audience"] == PUBLIC_AUDIENCE and kwargs["source_version"] == 1
    assert "teal palette fits" in content
    for canary in (_STEER_CANARY, _REPLY_CANARY):
        assert canary not in content

    assert memory.list(kind="fact") == []
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert memory.search(canary) == []


# ── tracing (copied helpers from tests/test_tracing.py) ───────────────────────────


def _reload_tracing():
    """Copied from tests/test_tracing.py: a fresh import of the real tracing.py."""
    import importlib.util
    import sys
    from pathlib import Path

    import observability

    if "tracing" in sys.modules:
        del sys.modules["observability.tracing"]
    real_path = Path(__file__).parents[1] / "src" / "observability" / "tracing.py"
    spec = importlib.util.spec_from_file_location("observability.tracing", real_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["observability.tracing"] = module
    spec.loader.exec_module(module)
    observability.tracing = module
    return module


def _enable_with_fake_client(tracing):
    """Copied from tests/test_tracing.py."""
    from unittest.mock import MagicMock

    fake = MagicMock()
    span = MagicMock()
    span.trace_id = "trace-abc"
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    child = MagicMock()
    fake.start_observation.return_value = child
    tracing._langfuse = fake
    tracing._enabled = True
    return fake, span, child


@pytest.fixture
def session_langfuse(monkeypatch):
    """Copied from tests/test_tracing.py::session_langfuse, folding in the module-restore guard
    (so a sibling test that captured `from observability import tracing` keeps the pristine one)."""
    import sys
    from unittest.mock import MagicMock

    import observability

    orig_mod = sys.modules.get("observability.tracing")
    orig_attr = getattr(observability, "tracing", None)
    tracing = _reload_tracing()
    fake = MagicMock()
    span = MagicMock()
    span.trace_id = "c" * 32
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    try:
        yield tracing, span
    finally:
        if orig_mod is not None:
            sys.modules["observability.tracing"] = orig_mod
        if orig_attr is not None:
            observability.tracing = orig_attr


def _bind_private_source(monkeypatch, *, traceable=False):
    """Copied from tests/test_tracing.py."""
    from graph import public_memory

    source = public_memory.PublicSource(
        version="retained-2", messages=(HumanMessage(content="Public request"),), traceable=traceable
    )
    monkeypatch.setattr(public_memory, "_source_reader", lambda sid: source if sid == "chat-private" else None)


@pytest.mark.asyncio
async def test_private_partition_records_no_trace_content_per_caller_kind(caller, session_langfuse, monkeypatch):
    """tests/test_tracing.py::test_a_private_partition_records_no_content_now_or_on_a_later_public_turn,
    re-run for each caller kind: a partitioned conversation records only content-free
    structure, now and on a later ordinary (incognito=False) turn. Suppression follows the
    retained source, so NO caller kind — not even the human operator — reads a canary off the
    trace; the gate is at projection, not at the reader."""
    tracing, span = session_langfuse
    _bind_private_source(monkeypatch)

    async with tracing.trace_session("chat-private", name="chat", input="PRIVATE_STEER_CANARY"):
        assert tracing.io_allowed() is False
        tracing.trace_tool_call("lookup", {"q": "PRIVATE_ARG_CANARY"}, "PRIVATE_RESULT_CANARY", 5, True)
        tracing.set_session_output("PRIVATE_REPLY_CANARY")
    async with tracing.trace_session("chat-private", name="chat", input="PRIVATE_LATER_CANARY", incognito=False):
        assert tracing.io_allowed() is False
        tracing.trace_tool_call("lookup", {"q": "PRIVATE_LATER_ARG"}, "PRIVATE_LATER_RESULT", 5, True)
        tracing.set_session_output("PRIVATE_LATER_REPLY")

    tool = tracing._langfuse.start_observation.call_args.kwargs
    assert (tool["input"], tool["output"]) == (None, None)
    blob = repr(span.update.call_args_list) + repr(tracing._langfuse.start_observation.call_args_list)
    assert "CANARY" not in blob


# ── prompt capture (copied helpers from tests/test_prompt_capture_middleware.py) ──


class _Req:
    """Copied from tests/test_prompt_capture_middleware.py."""

    def __init__(self, model_name, system_message, state=None):
        self.model = SimpleNamespace(model_name=model_name)
        self.system_message = system_message
        self.state = state or {}

    def override(self, **kw):
        r = _Req(self.model.model_name, self.system_message, self.state)
        for k, v in kw.items():
            setattr(r, k, v)
        return r


def _response(usage=None):
    """Copied from tests/test_prompt_capture_middleware.py."""
    return SimpleNamespace(result=[AIMessage(content="ok", usage_metadata=usage)])


def test_private_partition_captures_nothing_per_caller_kind(caller, monkeypatch):
    """tests/test_prompt_capture_middleware.py::test_private_partition_captures_nothing_and_drops_the_private_buffers,
    re-run for each caller kind: a private partition is suppressed before capture AND its
    private buffers (projected context + wire stash) are dropped, so nothing survives for a
    later call to pop — no caller kind can recover it, the human operator included."""
    from langchain_core.messages import SystemMessage

    from graph import public_memory
    from graph.context_frame import pop_projected_context, stash_projected_context
    from graph.middleware import wire_capture
    from graph.middleware.prompt_capture import PromptCaptureMiddleware
    from graph.middleware.request_context import request_metadata_scope
    from observability.prompt_snapshots import prompt_snapshots

    source = public_memory.PublicSource(version="v2", messages=(HumanMessage(content="pub"),))
    monkeypatch.setattr(public_memory, "_source_reader", lambda sid: source if sid == "chat-private" else None)
    monkeypatch.setattr("observability.tracing.current_session_id", lambda: "chat-private")

    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={})

    def inner(r):
        stash_projected_context("PRIVATE_CONTEXT_CANARY", [{"label": "Hot", "chars": 3}])
        wire_capture._WIRE_SYSTEM.set("PRIVATE_WIRE_CANARY")
        return _response()

    with request_metadata_scope({"a2a.task_id": f"task-private-{caller.kind}"}):
        capture.wrap_model_call(req, inner)

    assert prompt_snapshots().calls_for_task(f"task-private-{caller.kind}") == []
    assert pop_projected_context() == (None, None)
    assert wire_capture.pop_wire_system() is None


# ── fleet export (copied helpers from tests/test_trace_export.py) ─────────────────


class _Outcome:
    """Copied from tests/test_trace_export.py::_Outcome (as a plain class for ``vars()``)."""

    def __init__(self, context_id="sess-1"):
        self.context_id = context_id
        self.task_id = "task-1"
        self.state = "completed"
        self.models = ["claude-opus-4-8"]
        self.origin = "scheduler"
        self.trigger = "job-9"
        self.priority = ""
        self.cost_usd = 0.02
        self.duration_ms = 1500
        self.llm_calls = 2
        self.tool_calls = 1


_MESSAGES = [
    AIMessage(content="It's 72F and sunny in SF."),
]


def _enable_export(tmp_path, monkeypatch):
    """Copied from tests/test_trace_export.py::_enable."""
    from observability import trace_export

    monkeypatch.setenv("PROTOAGENT_FLEET_TRACE_EXPORT", str(tmp_path))
    trace_export.init()
    assert trace_export.is_enabled()


def _public_source(messages):
    """Copied from tests/test_trace_export.py::_public_source."""
    from graph.public_memory import FleetExport, PublicSource

    return PublicSource("retained-version-2", tuple(messages), fleet=FleetExport(
        task_id="task-1", orient="Public plan",
        tools=[{"type": "function", "function": {"name": "public_tool", "parameters": {}}}],
        metadata={**vars(_Outcome()), "teacher": "public-agent", "trace_id": "public-trace", "thinking": "on"},
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("context_id", ["sess-1", "background:proof-1"])
async def test_later_public_terminal_export_per_caller_kind(caller, server_op, tmp_path, monkeypatch, context_id):
    """tests/test_trace_export.py::test_later_public_terminal_export_uses_only_public_bytes,
    re-run for each caller kind: the fleet export of a later public terminal turn carries only
    public bytes. The private partition is written by the human operator; a peer caller kind
    CANNOT read it back (get_record returns no private payload), while the human-operator
    console recovers it — and the exported bytes are clean for every kind."""
    from langchain_core.messages import ToolMessage
    from langchain_core.messages import messages_from_dict, messages_to_dict

    from a2a.types import a2a_pb2
    from a2a_impl.stores import make_sqlite_engine, ReasoningCoalescingTaskStore
    from graph import public_memory
    from graph.compaction_engine import summarize_span, summary_message
    from observability import trace_export
    from runtime.state import STATE
    from server.a2a import _record_a2a_telemetry

    trace_export._reset_for_test()
    try:
        _enable_export(tmp_path, monkeypatch)
        operator = server_op  # the private partition is the operator's to write and recover
        db = str(tmp_path / "tasks.db")
        engine = make_sqlite_engine(db)
        store = ReasoningCoalescingTaskStore(engine)
        task = a2a_pb2.Task(id="task-1", context_id=context_id, history=[
            a2a_pb2.Message(role=a2a_pb2.ROLE_USER, parts=[a2a_pb2.Part(text="Public request")]),
        ])
        private = [HumanMessage(content="PRIVATE_STEERING_CANARY"), AIMessage(
            content="PRIVATE_REPLY_CANARY", tool_calls=[
                {"name": "private_tool", "args": {"text": "PRIVATE_ARGUMENT_CANARY"}, "id": "private-call"},
            ]), ToolMessage(content="PRIVATE_RESULT_CANARY", tool_call_id="private-call")]
        private_summary = summarize_span(private, lambda prompt: "PRIVATE_COMPACTION_CANARY")
        assert "PRIVATE_STEERING_CANARY" in private_summary
        await store.save_private(task, {"messages": messages_to_dict(private), "summary": private_summary}, operator)
        await engine.dispose()

        engine = make_sqlite_engine(db)
        store = ReasoningCoalescingTaskStore(engine)
        try:
            record = await store.get_record(task.id, operator)
            retained_private = messages_from_dict(record.private_payload["messages"])
            assert retained_private[0].content == "PRIVATE_STEERING_CANARY"

            # The real privacy gate per caller kind: a peer cannot read the private partition
            # back; only the human-operator console recovers it. Kept the operator-recovery
            # assertion; added the peer-denial assertion.
            as_caller = await store.get_record(task.id, caller.ctx)
            if caller.is_operator:
                assert as_caller is not None
                assert messages_from_dict(as_caller.private_payload["messages"])[0].content == "PRIVATE_STEERING_CANARY"
            else:
                assert as_caller is None or as_caller.private_payload is None

            public_messages = [HumanMessage(content=m.parts[0].text) for m in record.task.history]
            prompts = []

            def summarize(prompt):
                prompts.append(prompt)
                return "Public request summary"

            compacted = summary_message(summarize_span(public_messages, summarize))
            assert prompts and all("PRIVATE_" not in prompt for prompt in prompts)
            task.history.append(a2a_pb2.Message(role=a2a_pb2.ROLE_AGENT, parts=[a2a_pb2.Part(text="Public reply")]))
            await store.save(task, operator)
            assert (await store.get_record(task.id, operator)).private_payload["summary"] == private_summary
            source = _public_source([compacted, *_MESSAGES])
            monkeypatch.setattr(public_memory, "_source_reader", lambda sid: source)
            monkeypatch.setenv("AGENT_NAME", "PRIVATE_TEACHER_CANARY")
            monkeypatch.setattr(trace_export, "_tool_schema_cache", [{"description": "PRIVATE_SCHEMA_CANARY"}])
            monkeypatch.setattr("observability.tracing.current_trace_id", lambda: "PRIVATE_TRACE_CANARY")
            monkeypatch.setattr("graph.goals.store.GoalStore", lambda: SimpleNamespace(read_plan=lambda sid: "PRIVATE_PLAN_CANARY"))
            monkeypatch.setattr("server.turn_telemetry.record_turn", lambda **kwargs: None)
            monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(thinking="PRIVATE_CONFIG_CANARY"))
            monkeypatch.setattr(STATE, "graph", SimpleNamespace(bound_tools=[]))
            outcome = SimpleNamespace(**{**vars(_Outcome(context_id=context_id)), "usage": {}, "incognito": True,
                                        "models": ["PRIVATE_MODEL_CANARY"], "trigger": "PRIVATE_TRIGGER_CANARY"})
            checkpoint = {"channel_values": {"incognito": True, "messages": retained_private}}
            monkeypatch.setattr(STATE, "checkpointer", SimpleNamespace(get_tuple=lambda config: SimpleNamespace(checkpoint=checkpoint)))
            _record_a2a_telemetry(outcome)
            assert not list(tmp_path.glob("*.jsonl"))
            checkpoint["channel_values"].update(incognito=False, messages=[summary_message(private_summary), *_MESSAGES])
            outcome.incognito = False
            _record_a2a_telemetry(outcome)
        finally:
            await engine.dispose()

        files = list(tmp_path.glob("fleet-traces-*.jsonl"))
        assert len(files) == 1
        rows = [json.loads(line) for line in files[0].read_text().splitlines()]
        (row,) = rows
        exported = files[0].read_bytes()
        assert b"PRIVATE_" not in exported
        assert b"Public request summary" in exported
        assert row["meta"]["session_id"] == context_id
        assert row["meta"]["orient"] == "Public plan"
        assert row["meta"]["model"] == "claude-opus-4-8"
        assert row["tools"][0]["function"]["name"] == "public_tool"
        assert row["teacher"] == "public-agent"
        assert row["id"] == "fleet__public-trace"
    finally:
        trace_export._reset_for_test()
