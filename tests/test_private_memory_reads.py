"""Session memory respects private steering: the derived writers skip a conversation with
private turns, and the memory inspector reads hide it from callers that are not human operators."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph import session_gist
from graph.compaction_engine import summary_message
from graph.compaction_op import compact_thread
from graph.conversation_audience import is_private_message
from graph.conversation_harvest import harvest_thread
from graph.fence_scope import private_segment_scope
from graph.middleware.compaction import CountingSummarizationMiddleware
from graph.middleware.memory import SessionSummaryMiddleware
from graph.middleware.steering import SteeringMiddleware
from graph.private_sessions import public_part
from knowledge.history import HistoryStore
from operator_api.memory_routes import register_memory_routes
from graph import steering
from runtime.state import STATE
from tests._a2a_app import poll_terminal
from tests import test_private_steer_route as private_routes
from tests.test_private_steer_route import HUMANS, KINDS, REPLY, SECRET

rig, chat_mod = private_routes.rig, private_routes.chat_mod

pytestmark = [pytest.mark.asyncio, pytest.mark.platform_sensitive]

SID, OTHER = "chat-memory", "chat-other"
PRIVATE = {"additional_kwargs": {"audience": "human-operator"}}
PRIVATE_TURN = [HumanMessage(content=SECRET, **PRIVATE), AIMessage(content=REPLY)]
LATER_TURN = PRIVATE_TURN + [HumanMessage(content="ordinary"), AIMessage(content="ok")]
GIST_CONFIG = SimpleNamespace(memory_session_gist=True, memory_gist_idle_seconds=60)


@pytest.fixture
async def box(rig, tmp_path, monkeypatch):
    """The rig plus an isolated session-memory dir, a history store and the memory routes."""
    memory = tmp_path / "memory"
    monkeypatch.setenv("MEMORY_PATH", str(memory))
    monkeypatch.setattr("graph.middleware.memory._PERSISTENCE_DISABLED", False)
    history = HistoryStore(tmp_path / "agent.db")
    monkeypatch.setattr(STATE, "history_store", history, raising=False)
    register_memory_routes(rig.app)
    return SimpleNamespace(rig=rig, memory=memory, history=history)


async def _steer(rig, text, mid, **extra):
    task_id = (await rig.steer("shared-bearer", SID, text, mid, **extra)).json()["started_turn"]
    await poll_terminal(rig.op, task_id)


def _retained(box) -> str:
    """Everything peer-readable session memory holds: every memory file and history row."""
    files = [p.read_text(encoding="utf-8") for p in Path(box.memory).rglob("*") if p.is_file()]
    rows = [e.content for e in box.history.list_for_session(SID, include_private=True)]
    return "\n".join(files + rows)


async def _run_writers(state):
    """Every derived writer a finished turn or an idle conversation can reach."""
    SessionSummaryMiddleware(config=GIST_CONFIG).after_agent(state, None)
    session_gist.cancel(session_gist.card_path(state))
    await CountingSummarizationMiddleware(
        model=FakeListChatModel(responses=["summary"]), trigger=("messages", 1), keep=("messages", 1)
    ).abefore_model(state, None)
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values=state)), aupdate_state=AsyncMock())
    summarizer = AsyncMock(return_value="summary")
    compacted = await compact_thread(graph, object(), None, object(), SID, SID, keep_recent=0, summarizer=summarizer)
    checkpointer = SimpleNamespace(
        aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={"channel_values": state}, metadata={}))
    )
    harvested = await harvest_thread(
        SID, checkpointer=checkpointer, knowledge_store=None, config=object(), summarizer=summarizer
    )
    return compacted, harvested


async def test_private_turn_writes_no_session_memory(box):
    await _steer(box.rig, SECRET, "private", private=True)
    with private_segment_scope(lambda: True):  # the private turn's own after_agent
        SessionSummaryMiddleware().after_agent({"session_id": SID, "messages": PRIVATE_TURN}, None)
    assert SECRET not in _retained(box) and REPLY not in _retained(box)


async def test_later_ordinary_turn_writes_no_private_text(box, monkeypatch):
    await _steer(box.rig, SECRET, "private", private=True)
    await _steer(box.rig, "ordinary", "public")
    scheduled = []
    monkeypatch.setattr(session_gist, "Timer", lambda *a, **kw: scheduled.append(a) or SimpleNamespace(start=bool, cancel=bool))
    compacted, harvested = await _run_writers({"session_id": SID, "messages": LATER_TURN})
    assert SECRET not in _retained(box) and REPLY not in _retained(box)
    assert harvested is None and scheduled == []
    # Both compaction archives (automatic, then manual) still record the ordinary turn.
    archives = [e.content for e in box.history.list_for_session(SID, include_private=True)]
    assert not compacted["refused"] and len(archives) == 2
    assert all("ordinary" in a and "ok" in a for a in archives)


@pytest.mark.parametrize("next_private", [False, True])
async def test_pre_marker_private_turn_withholds_shared_writers(box, monkeypatch, next_private):
    # Real authentication and two or three durable turn completions make this slower than a unit test.
    await _steer(box.rig, SECRET, "legacy-private", private=True)
    # Pre-deploy tasks have private ownership, but no marker or message stamps.
    for path in (box.memory / ".private-sessions").iterdir():
        path.unlink()
    if next_private:
        await _steer(box.rig, "new private input", "new-private", private=True)
    await _steer(box.rig, "ordinary", "public")
    scheduled = []
    monkeypatch.setattr(session_gist, "Timer", lambda *a, **kw: scheduled.append(a) or SimpleNamespace(start=bool, cancel=bool))
    state = {"session_id": SID, "messages": [HumanMessage(content=SECRET), AIMessage(content=REPLY)] + LATER_TURN[2:]}
    _, harvested = await _run_writers(state)
    assert SECRET not in _retained(box) and REPLY not in _retained(box)
    assert harvested is None and scheduled == []


@pytest.mark.parametrize("missing_engine", [True, False])
async def test_unknown_task_ownership_withholds_shared_writers(box, monkeypatch, missing_engine):
    from sqlalchemy.engine import URL

    engine = None if missing_engine else SimpleNamespace(url=URL.create("sqlite+aiosqlite", database=str(box.memory / "missing.db")))
    monkeypatch.setattr(STATE, "a2a_task_engine", engine)
    state = {"session_id": SID, "messages": [HumanMessage(content=SECRET), AIMessage(content=REPLY)]}
    _, harvested = await _run_writers(state)
    assert SECRET not in _retained(box) and REPLY not in _retained(box)
    assert harvested is None


async def test_private_turn_input_is_stamped(box):
    """What a private turn puts into the checkpoint — its input and a steer folded into it —
    carries the private audience; an ordinary turn's input is built exactly as before."""
    plain = chat_mod._vision_human_message("ordinary", [], session_id=SID, incognito=False)
    assert set(plain.additional_kwargs) == {"protoagent_sent_at"}
    with private_segment_scope(lambda: True):
        assert is_private_message(chat_mod._vision_human_message(SECRET, [], session_id=SID, incognito=False))
        steering.enqueue(SID, SECRET, audience=steering.AUDIENCE_PRIVATE)
        folded = SteeringMiddleware._inject({"session_id": SID, "messages": []})
    assert is_private_message(folded["messages"][0])
    steering.enqueue(SID, "ordinary")
    ordinary = SteeringMiddleware._inject({"session_id": SID, "messages": []})["messages"][0]
    assert set(ordinary.additional_kwargs) == {"protoagent_sent_at"}


async def test_archive_drops_whole_private_turns(box):
    public = [HumanMessage(content="ordinary"), AIMessage(content="ok")]
    tool = ToolMessage(content=REPLY, tool_call_id="t")
    fold = HumanMessage(content=SECRET, **PRIVATE)
    turn = [PRIVATE_TURN[0], tool, fold, PRIVATE_TURN[1]]
    assert public_part(public + turn + public, SID) == public + public  # the stamps alone decide
    summarized = [summary_message("s")] + public
    assert public_part(public, SID) == public and public_part(summarized, SID) == summarized  # no private turn
    await _steer(box.rig, SECRET, "private", private=True)
    # Marked: a summary may have folded a private turn, and the kept tail may begin inside it.
    compacted = [summary_message(SECRET), AIMessage(content=REPLY)]
    assert public_part(compacted + public, SID) == public
    assert public_part(public, "another-session") == public


async def test_gist_is_withheld_from_an_earlier_public_card(box, monkeypatch):
    """A card written before the private turn stays public-only: no gist is derived from
    the later transcript, and the card itself is not rewritten."""
    await _steer(box.rig, "ordinary", "public")
    first = {"session_id": SID, "messages": [HumanMessage(content="ordinary"), AIMessage(content="ok")] * 3}
    SessionSummaryMiddleware().after_agent(first, None)
    card = (box.memory / f"{SID}.json").read_text(encoding="utf-8")
    await _steer(box.rig, SECRET, "private", private=True)
    scheduled = []
    monkeypatch.setattr(session_gist, "Timer", lambda *a, **kw: scheduled.append(a) or SimpleNamespace(start=bool, cancel=bool))
    session_gist.schedule({"session_id": SID, "messages": first["messages"] + LATER_TURN}, GIST_CONFIG)
    assert scheduled == [] and (box.memory / f"{SID}.json").read_text(encoding="utf-8") == card


async def test_unmarked_session_writers_are_unchanged(box):
    await _steer(box.rig, "ordinary", "public")
    state = {"session_id": SID, "messages": [HumanMessage(content="ordinary"), AIMessage(content="ok")]}
    compacted, harvested = await _run_writers(state)
    assert json.loads((box.memory / f"{SID}.json").read_text(encoding="utf-8"))["messages"][0]["content"] == "ordinary"
    assert not compacted["refused"] and harvested is not None
    assert not (box.memory / ".private-sessions").exists()


CASES = [(True, True), (True, False), (False, False)]


@pytest.fixture
async def leaked(box, request):
    """A box whose session memory ALREADY holds the conversation's text (what an earlier
    build retained), so the reads are judged on their own guard."""
    flag, private = request.param
    box.rig.config.private_steering_enabled = flag
    await _steer(box.rig, "ordinary", "public")
    if private:
        await _steer(box.rig, SECRET, "private", private=True)
    box.memory.mkdir(parents=True, exist_ok=True)
    for sid, text in ((SID, SECRET if private else "ordinary"), (OTHER, "elsewhere")):
        card = {"session_id": sid, "trace_id": "t", "messages": [{"role": "user", "content": text}],
                "message_count": 2, "tool_calls": [], "final_output": "ok", "timestamp": "2026-11-18T00:00:00+00:00"}
        (box.memory / f"{sid}.json").write_text(json.dumps(card), encoding="utf-8")
        box.entry = box.history.add(f"user: {text}", kind="archive", session_id=sid, heading="Conversation archive")
    box.private, box.text = private, SECRET if private else "ordinary"
    return box


@pytest.mark.parametrize("leaked", CASES, indirect=True)
@pytest.mark.parametrize("kind", KINDS)
async def test_memory_reads(leaked, kind):
    client, hidden = leaked.rig.clients[kind], leaked.private and kind not in HUMANS
    detail = await client.get(f"/api/memory/sessions/{SID}")
    listing = await client.get("/api/memory/sessions")
    search = await client.get("/api/memory/history/search", params={"q": leaked.text})
    rollup = await client.get("/api/memory/history/sessions")
    if kind == "federation":  # the existing operator API ceiling answers first
        assert {r.status_code for r in (detail, listing, search, rollup)} == {403}
        return
    assert listing.status_code == search.status_code == rollup.status_code == 200
    listed = [s["session_id"] for s in listing.json()["sessions"]]
    hits = [h["session_id"] for h in search.json()["results"]]
    rolled = [s["session_id"] for s in rollup.json()["sessions"]]
    if hidden:
        assert detail.status_code == 403 and detail.json() == {"detail": "Session read unavailable."}
        assert listed == [OTHER] and hits == [] and rolled == [OTHER]
        # (the search response echoes the caller's own ``query``; its results are empty)
        assert all(SECRET not in r.text for r in (detail, listing, rollup))
    else:
        assert detail.status_code == 200 and leaked.text in detail.text
        assert sorted(listed) == [SID, OTHER] and hits == [SID] and sorted(rolled) == [SID, OTHER]
        assert leaked.text in listing.text and leaked.text in search.text


@pytest.mark.parametrize("leaked", [(True, True)], indirect=True)
@pytest.mark.parametrize("kind", ["fleet-service", "paired-agent", "shared-bearer"])
async def test_history_entry_read(leaked, kind):
    entries = leaked.history.list_for_session(SID)
    response = await leaked.rig.clients[kind].get(f"/api/memory/history/entries/{entries[0].id}")
    if kind in HUMANS:
        assert response.status_code == 200 and SECRET in response.text
    else:
        assert response.status_code == 403 and response.json() == {"detail": "Session read unavailable."}


@pytest.mark.parametrize("leaked", [(True, True)], indirect=True)
async def test_missing_task_store_fails_closed(leaked, monkeypatch):
    monkeypatch.setattr(STATE, "a2a_task_engine", None)
    for path in ("/api/memory/sessions", f"/api/memory/sessions/{SID}", f"/api/memory/history/search?q={SECRET}"):
        response = await leaked.rig.clients["fleet-service"].get(path)
        assert response.status_code == 503 and SECRET not in response.text
        assert (await leaked.rig.op.get(path)).status_code == 200
