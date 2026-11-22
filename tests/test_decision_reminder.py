"""The operator is reminded once, in a reply, that a no-pause decision is still waiting (#5678 7b)."""

import importlib
from typing import Annotated, TypedDict

import pytest
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from graph.config import LangGraphConfig
from graph.decisions import DecisionStore
from graph.middleware.request_context import verified_principal_scope
from runtime.state import STATE
from server.turn_sync import _native_turn

chat = importlib.import_module("server.chat")
control = importlib.import_module("server.turn_control")

THREAD = "a2a:s"
OPTIONS = [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]
REMINDER = 'Still waiting on your decision: "Deploy now?"'


class State(TypedDict):
    messages: Annotated[list, add_messages]


@pytest.fixture
def store(monkeypatch):
    ledger = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", ledger)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig())
    monkeypatch.setattr(STATE, "goal_controller", None)
    monkeypatch.setattr("graph.decisions._notify_created", lambda rec: None)

    def agent(state, config):
        text = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        if text == "ask":
            ledger.create("s", "Ship the docs?", OPTIONS, thread_id=config["configurable"]["thread_id"])
        return {"messages": [AIMessage(content="Finished.")]}

    builder = StateGraph(State)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    monkeypatch.setattr(STATE, "graph", builder.compile(checkpointer=InMemorySaver()))
    return ledger


def ask(store, question="Deploy now?", **kwargs):
    return store.create("s", question, OPTIONS, thread_id=THREAD, **kwargs)


async def sync_reply(text, *, hitl_resume=False):
    reply = await _native_turn(
        text, None, session_id="s", config={"configurable": {"thread_id": THREAD}},
        usage_cb=UsageMetadataCallbackHandler(), state_extra={}, tool_fence=None,
        hitl_resume=hitl_resume, incognito=False,
    )
    return reply[-1]["content"]


async def stream_reply(text, *, request_metadata=None):
    """The streaming driver's reply: the ``done`` text, checked against the live text frames."""
    config = {"configurable": {"thread_id": THREAD}}
    streamed, done = "", None
    async with control._thread_lock(THREAD):
        async for kind, payload in chat._run_native_turn(text, "s", config, request_metadata=request_metadata or {}):
            if kind == "text":
                streamed += payload
            elif kind == "done":
                done = payload
    assert done is not None
    assert (REMINDER in streamed) == (REMINDER in done)
    return done


DRIVERS = {"sync": sync_reply, "stream": stream_reply}


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", DRIVERS)
async def test_first_unrelated_operator_turn_is_reminded_once(store, driver):
    rec = ask(store)
    turn = DRIVERS[driver]
    with verified_principal_scope("shared-operator-bearer"):
        first = await turn("what's the weather?")
        # The reply's own text, then the line as its own final paragraph.
        assert first.endswith(f"\n\n{REMINDER}") and first.count(REMINDER) == 1
        assert store.get(rec.id).reminded_at
        second = await turn("and tomorrow?")
    assert "Still waiting" not in second
    assert store.get(rec.id).state == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", DRIVERS)
async def test_asking_turn_has_no_reminder(store, driver):
    turn = DRIVERS[driver]
    with verified_principal_scope("shared-operator-bearer"):
        asked = await turn("ask")
        (rec,) = store.pending_unpaused(THREAD)
        assert "Still waiting" not in asked
        assert store.get(rec.id).reminded_at is None
        # The next unrelated turn is the first that may remind.
        assert (await turn("hello")).endswith('Still waiting on your decision: "Ship the docs?"')


@pytest.mark.asyncio
async def test_typed_answering_turn_has_no_reminder(store):
    rec = ask(store)
    with verified_principal_scope("shared-operator-bearer"):
        reply = await sync_reply("Yes")
    assert store.get(rec.id).state == "answered"
    assert "Still waiting" not in reply
    assert store.get(rec.id).reminded_at is None


@pytest.mark.asyncio
async def test_delivery_turn_has_no_reminder_for_another_open_decision(store):
    answered = ask(store, "Pick a region?")
    store.answer(answered.id, "yes", answer_kind="option")
    other = ask(store)
    with verified_principal_scope("shared-operator-bearer"):
        reply = await stream_reply("deliver", request_metadata={"decision_id": answered.id})
        assert "Still waiting" not in reply
        assert store.get(other.id).reminded_at is None
        assert (await stream_reply("unrelated")).endswith(REMINDER)


@pytest.mark.asyncio
async def test_resume_turn_has_no_reminder(store):
    rec = ask(store)
    with verified_principal_scope("shared-operator-bearer"):
        reply = await sync_reply("continue", hitl_resume=True)
    assert "Still waiting" not in reply
    assert store.get(rec.id).reminded_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", DRIVERS)
@pytest.mark.parametrize("principal", ["federation", "fleet-service", "agent-device"])
async def test_peer_caller_is_never_reminded(store, driver, principal):
    rec = ask(store)
    with verified_principal_scope(principal):
        reply = await DRIVERS[driver]("status?")
    assert "Still waiting" not in reply and "Deploy now?" not in reply
    assert store.get(rec.id).reminded_at is None
    # The operator's own first unrelated turn still gets it.
    with verified_principal_scope("shared-operator-bearer"):
        assert (await DRIVERS[driver]("status?")).endswith(REMINDER)


@pytest.mark.asyncio
async def test_autonomous_turn_is_never_reminded(store):
    rec = ask(store)
    reply = await stream_reply("tick", request_metadata={"origin": "scheduler"})
    assert "Still waiting" not in reply
    assert store.get(rec.id).reminded_at is None


@pytest.mark.asyncio
async def test_pause_id_decision_is_untouched(store):
    rec = ask(store, interrupt_id="pause-1")
    with verified_principal_scope("shared-operator-bearer"):
        reply = await sync_reply("hello")
    assert "Still waiting" not in reply
    assert store.get(rec.id).reminded_at is None


def test_claim_reminder_is_once_per_decision_oldest_first(store):
    first, second = ask(store), ask(store, "Ship the docs?")
    later = "9999-01-01T00:00:00+00:00"
    assert store.claim_reminder(THREAD, later).id == first.id
    assert store.claim_reminder(THREAD, later).id == second.id
    assert store.claim_reminder(THREAD, later) is None
    assert store.claim_reminder("", later) is None
