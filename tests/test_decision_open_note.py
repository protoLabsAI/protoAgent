"""An open no-pause decision is named to the model on every call, model-only (#5678 7a)."""

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from graph.decisions import DecisionStore
from graph.middleware.decision_context import OPEN_NOTE_GUARD, DecisionContextMiddleware
from graph.middleware.guard_notes import is_guard_note
from graph.middleware.request_context import verified_principal_scope
from observability.tracing import reset_trust_tier, set_trust_tier
from runtime.state import STATE

THREAD = "chat-one:scoped"
OPTIONS = [{"id": "go", "label": "Release"}]
CANNOT = "The caller of this turn is not the operator and cannot answer it."


class RecordingModel(GenericFakeChatModel):
    """Fake model that keeps every call's input messages."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.fixture
def store(monkeypatch):
    ledger = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", ledger)
    return ledger


def ask(store, question="Release now?", **kwargs):
    return store.create("chat-one", question, OPTIONS, thread_id=kwargs.pop("thread_id", THREAD), **kwargs)


def run(turns=1, thread=THREAD):
    model = RecordingModel(messages=iter([AIMessage(content="Noted.")] * (turns + 2)))
    model.seen = []
    agent = create_agent(model, middleware=[DecisionContextMiddleware()])
    config = {"configurable": {"thread_id": thread}}
    results = [agent.invoke({"messages": [HumanMessage(f"Turn {i}")]}, config) for i in range(turns)]
    return model.seen, results


def open_notes(messages):
    return [m for m in messages if is_guard_note(m, OPEN_NOTE_GUARD)]


def test_every_operator_turn_names_the_open_question(store):
    ask(store)
    with verified_principal_scope("shared-operator-bearer"):
        seen, results = run(turns=3)
    assert len(seen) == 3
    for call in seen:
        (note,) = open_notes(call)
        assert call[-1] is note
        assert "Question: Release now?" in note.content
        assert CANNOT not in note.content
    # Model-only: the note never lands in the saved thread the operator's view reads.
    assert not open_notes(results[-1]["messages"])


def test_local_turn_counts_as_operator(store):
    ask(store)
    seen, _ = run()
    (note,) = open_notes(seen[0])
    assert CANNOT not in note.content


def test_every_open_question_is_named(store):
    ask(store, "Release now?")
    ask(store, "Ship the docs too?")
    seen, _ = run()
    (note,) = open_notes(seen[0])
    assert note.content.index("Question: Release now?") < note.content.index("Question: Ship the docs too?")


@pytest.mark.parametrize("case", ["none", "paused", "other_thread", "answered", "withdrawn"])
def test_no_note_without_an_open_no_pause_decision(store, case):
    if case == "paused":
        ask(store, interrupt_id="pause-id")
    elif case == "other_thread":
        ask(store, thread_id="chat-one:other")
    elif case == "answered":
        store.answer(ask(store).id, "go", answer_kind="option")
    elif case == "withdrawn":
        store.withdraw(ask(store).id)
    seen, _ = run()
    assert not any(open_notes(call) for call in seen)


def test_note_stops_once_answered(store):
    rec = ask(store)
    model = RecordingModel(messages=iter([AIMessage(content="Noted.")] * 4))
    model.seen = []
    agent = create_agent(model, middleware=[DecisionContextMiddleware()])
    config = {"configurable": {"thread_id": THREAD}}
    agent.invoke({"messages": [HumanMessage("one")]}, config)
    store.answer(rec.id, "go", answer_kind="option")
    agent.invoke({"messages": [HumanMessage("two")]}, config)
    assert open_notes(model.seen[0]) and not open_notes(model.seen[1])


@pytest.mark.parametrize("principal", ["fleet-service", "agent-device", "federation-peer"])
def test_non_operator_caller_is_told_it_cannot_answer(store, principal):
    ask(store)
    with verified_principal_scope(principal):
        seen, _ = run()
    (note,) = open_notes(seen[0])
    assert "Question: Release now?" in note.content
    assert CANNOT in note.content
    assert "Do not ask this caller to answer or decide it." in note.content


def test_federation_tier_without_principal_cannot_answer(store):
    ask(store)
    token = set_trust_tier("federation")
    try:
        seen, _ = run()
    finally:
        reset_trust_tier(token)
    (note,) = open_notes(seen[0])
    assert CANNOT in note.content


@pytest.mark.asyncio
async def test_async_call_carries_note(store):
    ask(store)
    model = RecordingModel(messages=iter([AIMessage(content="Noted.")] * 2))
    model.seen = []
    agent = create_agent(model, middleware=[DecisionContextMiddleware()])
    await agent.ainvoke({"messages": [HumanMessage("go")]}, {"configurable": {"thread_id": THREAD}})
    (note,) = open_notes(model.seen[0])
    assert "Question: Release now?" in note.content


def test_ledger_failure_leaves_model_input_alone(store, monkeypatch, caplog):
    def fail(thread_id):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(store, "pending_unpaused", fail)
    seen, _ = run()
    assert not open_notes(seen[0])
    assert "Could not read open decisions" in caplog.text


def test_helpers_return_open_and_unreminded_decisions(store):
    assert store.open_unpaused_decision(THREAD) is None
    assert store.unreminded_unpaused_decision(THREAD) is None
    assert store.open_unpaused_decision("") is None
    ask(store, "Paused?", interrupt_id="pause-id")
    ask(store, "Elsewhere?", thread_id="chat-one:other")
    first = ask(store, "Release now?")
    second = ask(store, "Ship the docs too?")

    assert store.open_unpaused_decision(THREAD).id == first.id
    assert store.unreminded_unpaused_decision(THREAD).id == first.id

    store.mark_reminded(first.id)
    assert store.open_unpaused_decision(THREAD).id == first.id
    assert store.unreminded_unpaused_decision(THREAD).id == second.id

    store.mark_reminded(second.id)
    assert store.open_unpaused_decision(THREAD).id == first.id
    assert store.unreminded_unpaused_decision(THREAD) is None

    store.answer(first.id, "go", answer_kind="option")
    store.cancel(second.id)
    assert store.open_unpaused_decision(THREAD) is None
    assert [r.id for r in store.pending_unpaused(THREAD)] == []
