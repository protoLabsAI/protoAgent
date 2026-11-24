"""``decisions.never_block`` (#5678 slice 9a): request_decision records and returns, no pause.

With the setting on, the tool writes a no-pause ledger record (empty interrupt id), returns
"asked, waiting", and the model then writes the turn's reply itself (what it did, what it
is asking); the turn ends completed rather than input-required. A later unrelated turn runs normally, the decision stays pending, and the
operator can still answer it. Since #5678 slice 10 the tool never pauses, so the setting is
declared but has no effect: with it off (the default) the ask is the same no-pause record.

Driven through both real turn drivers on a real ``create_agent`` graph with an
in-memory checkpoint, a scripted fake model and an in-memory ``DecisionStore``.
"""

from __future__ import annotations

import importlib
import uuid

import pytest
from langchain.agents import create_agent
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from graph.config import LangGraphConfig
from graph.decisions import DecisionStore
from graph.middleware.request_context import verified_principal_scope
from graph.state import ProtoAgentState
from runtime.state import STATE
from tools.lg_tools import get_all_tools, request_decision

turn_control = importlib.import_module("server.turn_control")

SESSION = "s-never-block"
ASK = {"question": "Deploy to prod?", "options": ["yes", "no"]}
WRAP = "I checked the release and asked you whether to deploy it to prod."


class ScriptedModel(GenericFakeChatModel):
    """Fake chat model that replies from a script and keeps every call's input."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _ask_call() -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "request_decision", "args": ASK, "id": "call-ask"}])


@pytest.fixture
def store(monkeypatch) -> DecisionStore:
    from graph import steering

    monkeypatch.setattr(steering, "_QUEUES", {})
    monkeypatch.setattr(steering, "_QUEUE_TOUCHED", {})
    ledger = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", ledger, raising=False)
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    return ledger


def _graph(monkeypatch, *, never_block: bool, replies: list[AIMessage]):
    """The live config and a real agent graph bound with the tool list get_all_tools builds."""
    config = LangGraphConfig(identity_name="deploy-agent", decisions_never_block=never_block)
    monkeypatch.setattr(STATE, "graph_config", config, raising=False)
    tool = next(t for t in get_all_tools(graph_config=config) if t.name == "request_decision")
    model = ScriptedModel(messages=iter(replies), disable_streaming=True)
    model.seen = []
    graph = create_agent(model, tools=[tool], state_schema=ProtoAgentState, checkpointer=InMemorySaver())
    monkeypatch.setattr(STATE, "graph", graph)
    return graph, model


async def _turn(thread: str, message: str, *, origin: str = "api-chat", sink: dict | None = None):
    from server.turn_sync import _native_turn

    with verified_principal_scope("shared-operator-bearer"):
        return await _native_turn(
            message, None, session_id=SESSION, config={"configurable": {"thread_id": thread}},
            usage_cb=UsageMetadataCallbackHandler(), state_extra={}, tool_fence=None,
            hitl_resume=False, incognito=False, origin=origin, telemetry_sink=sink,
        )


def _thread() -> str:
    # One thread per test: the per-thread turn lock binds to the first event loop it meets.
    return f"a2a:{SESSION}:{uuid.uuid4().hex}"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["api-chat", "scheduler"])
async def test_the_asking_turn_ends_completed_with_one_no_pause_record(store, monkeypatch, origin):
    graph, model = _graph(monkeypatch, never_block=True, replies=[_ask_call(), AIMessage(content=WRAP)])
    thread, sink = _thread(), {}
    reply = await _turn(thread, "ship it", origin=origin, sink=sink)

    # The model replies after the ask (not return_direct): two model calls, no interrupt, not parked.
    assert len(model.seen) == 2
    assert sink.get("state") != "input_required"
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread}})
    assert not snapshot.next and not snapshot.interrupts
    rec, = store.list(state="pending")
    assert rec.interrupt_id == ""
    assert (rec.thread_id, rec.session_id, rec.agent) == (thread, SESSION, "deploy-agent")
    assert rec.question == "Deploy to prod?"
    # The tool's "asked, waiting" result names the record and tells the model to wrap up.
    last = snapshot.values["messages"][-2]
    assert isinstance(last, ToolMessage) and last.content.startswith("Asked, waiting")
    assert "End this turn now with no more tool calls" in last.content
    assert isinstance(model.seen[1][-1], ToolMessage)
    assert rec.id in last.content
    assert 'Address the operator as "you"' in last.content
    assert "never by name or in the third person" in last.content
    assert "Never show decision ids in a reply" in last.content
    # The reply is a completed one carrying the decision card, not an "Input needed" park.
    # The reply is the model's own words about this turn, not one fixed line.
    assert reply[0]["content"] == WRAP
    assert reply[0]["hitl"]["decision_id"] == rec.id


@pytest.mark.asyncio
async def test_an_ask_with_no_model_text_falls_back_to_naming_the_question(store, monkeypatch):
    _graph(monkeypatch, never_block=True, replies=[_ask_call(), AIMessage(content="")])
    reply = await _turn(_thread(), "ship it")
    rec, = store.list(state="pending")
    assert reply[0]["content"] == "I've asked you: Deploy to prod?"
    assert rec.id not in reply[0]["content"]
    assert "do not ask again" not in reply[0]["content"]


@pytest.mark.asyncio
async def test_streaming_no_pause_ask_replies_without_model_instructions(store, monkeypatch):
    chat = importlib.import_module("server.chat")
    _graph(monkeypatch, never_block=True, replies=[_ask_call(), AIMessage(content="")])
    with verified_principal_scope("shared-operator-bearer"):
        frames = [frame async for frame in chat._run_native_turn(
            "ship it", SESSION, {"configurable": {"thread_id": _thread()}},
        )]

    rec, = store.list(state="pending")
    reply = next(payload for kind, payload in frames if kind == "done")
    assert reply == "I've asked you: Deploy to prod?"
    assert rec.id not in reply


@pytest.mark.asyncio
async def test_a_later_turn_leaves_the_decision_pending_and_answerable(store, monkeypatch):
    graph, model = _graph(monkeypatch, never_block=True, replies=[
        _ask_call(), AIMessage(content=WRAP), AIMessage(content="It is 4."), AIMessage(content="Deploying now."),
    ])
    thread = _thread()
    await _turn(thread, "ship it")
    rec, = store.list(state="pending")

    # A later unrelated message runs as an ordinary turn: not held behind the decision.
    reply = await _turn(thread, "what is 2 + 2?")
    assert reply[0]["content"].startswith("It is 4.")
    assert "Input needed" not in reply[0]["content"]
    # ...and the operator is reminded once that the decision is still open (#5678 7b).
    assert 'Still waiting on your decision: "Deploy to prod?"' in reply[0]["content"]
    assert len(model.seen) == 3
    assert isinstance(model.seen[2][-1], HumanMessage)
    assert store.get(rec.id).pending

    # The decision is still answerable: the operator's typed pick answers it and the
    # delivery turn hands the saved answer to the agent.
    reply = await _turn(thread, "yes")
    answered = store.get(rec.id)
    assert (answered.state, answered.answer) == ("answered", "yes")
    assert answered.delivered_at
    assert reply[0]["content"] == "Deploying now."
    assert "Deploy to prod?" in model.seen[3][-1].content
    assert store.list(state="pending") == []


@pytest.mark.asyncio
async def test_with_the_setting_off_request_decision_no_longer_pauses(store, monkeypatch):
    # #5678 slice 10 removed the pausing path: the setting off is the same no-pause ask.
    assert LangGraphConfig().decisions_never_block is False
    graph, model = _graph(monkeypatch, never_block=False,
                          replies=[_ask_call(), AIMessage(content=WRAP), AIMessage(content="It is 4.")])
    tool = next(t for t in get_all_tools(graph_config=LangGraphConfig()) if t.name == "request_decision")
    assert not tool.return_direct
    thread, sink = _thread(), {}
    reply = await _turn(thread, "ship it", sink=sink)

    assert "Input needed" not in reply[0]["content"]
    assert sink.get("state") != "input_required"
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread}})
    assert not snapshot.next and not snapshot.interrupts
    rec, = store.list(state="pending")
    assert rec.interrupt_id == ""
    # A fresh message is not held behind the decision.
    later = await _turn(thread, "what is 2 + 2?")
    assert "Input needed first" not in later[0]["content"]


def test_the_setting_round_trips_and_renders_in_settings():
    from graph.config_io import config_to_dict
    from graph.settings_schema import FIELDS

    assert LangGraphConfig.from_dict({}).decisions_never_block is False
    config = LangGraphConfig.from_dict({"decisions": {"never_block": True}})
    assert config.decisions_never_block is True
    assert config_to_dict(config)["decisions"] == {"never_block": True}
    field = next(f for f in FIELDS if f.key == "decisions.never_block")
    assert (field.attr, field.type, field.restart, field.ui_hidden) == ("decisions_never_block", "bool", False, False)


@pytest.mark.parametrize("args, error", [
    ({"options": []}, "Error:"),
    ({"expires_in_minutes": 5}, "Error: pass both expires_in_minutes and default_option, or neither"),
    ({"default_option": "maybe", "expires_in_minutes": 5}, "Error: default_option must name an option id"),
])
def test_invalid_asks_are_refused_before_any_record(store, monkeypatch, args, error):
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(decisions_never_block=True), raising=False)
    result = request_decision.invoke({**ASK, **args, "state": {"session_id": SESSION}})
    assert result.startswith(error)
    assert store.list() == []


def test_outside_a_graph_run_there_is_no_thread_to_ask_on(store, monkeypatch):
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(decisions_never_block=True), raising=False)
    result = request_decision.invoke({**ASK, "state": {"session_id": SESSION}})
    assert result.startswith("Error: request_decision needs a chat thread")
    assert store.list() == []


@pytest.mark.asyncio
async def test_a_no_pause_record_never_holds_an_autonomous_turn(store, monkeypatch):
    # The hold keys a decision park on the checkpoint's interrupt id; an unresolved (empty)
    # id must not match a no-pause record and park an autonomous turn behind it.
    store.create(SESSION, "Deploy to prod?", ["yes", "no"], thread_id="a2a:held", interrupt_id="")
    monkeypatch.setattr(turn_control, "_chat", lambda: type("C", (), {
        "_pending_interrupt": staticmethod(_no_interrupt_id),
    })())
    assert await turn_control._decision_interrupt_pending({"configurable": {"thread_id": "a2a:held"}}) is False


async def _no_interrupt_id(config):
    return ("", {"kind": "decision"})
