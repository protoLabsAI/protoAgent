"""Missed no-pause answers reach the model once from the durable ledger."""

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from graph.decisions import DecisionStore
from graph.middleware.decision_context import DecisionContextMiddleware
from graph.middleware.guard_notes import is_guard_note
from runtime.state import STATE

THREAD = "chat-one:scoped"
OPTIONS = [{"id": "go", "label": "Release"}]


class Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def store(monkeypatch):
    ledger = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", ledger)
    return ledger


def ask(store, **kwargs):
    return store.create("chat-one", "Release now?", OPTIONS, thread_id=kwargs.pop("thread_id", THREAD), **kwargs)


def graph():
    return create_agent(Model(messages=iter([AIMessage(content="Noted.")] * 4)),
                        middleware=[DecisionContextMiddleware()])


def notes(result):
    return [m for m in result["messages"] if is_guard_note(m, "decision-context")]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,kind,expected", [
    ("go", "option", "Operator chose: Release (go)"),
    ("Wait for the morning", "text", "Wait for the morning"),
])
async def test_main_agent_recovers_ledger_answer_once(store, monkeypatch, answer, kind, expected):
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig

    rec = store.answer(ask(store).id, answer, answer_kind=kind)
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **kw: Model(
        messages=iter([AIMessage(content="Noted.")] * 4)))
    agent = create_agent_graph(LangGraphConfig(), include_subagents=False)
    config = {"configurable": {"thread_id": THREAD}}
    state = {"messages": [HumanMessage("Continue; the answer is NOT this message.")], "session_id": "chat-one"}
    first = await agent.ainvoke(state, config)
    assert [m.content for m in notes(first)] == [
        f"The operator answered your decision.\nQuestion: Release now?\nAnswer: {expected}"
    ]
    stamp = store.get(rec.id).delivered_at
    assert stamp
    assert not notes(await agent.ainvoke(state, config))
    assert store.get(rec.id).delivered_at == stamp


@pytest.mark.parametrize("case", ["delivered", "pending", "paused", "other_thread", "cancelled", "withdrawn", "expired"])
def test_irrelevant_records_do_not_add_context(store, case):
    kwargs = {"interrupt_id": "pause-id"} if case == "paused" else {}
    if case == "other_thread":
        kwargs["thread_id"] = "chat-one:other"
    if case == "expired":
        kwargs.update(expires_at="2000-01-01T00:00:00+00:00", default_option="go")
    rec = ask(store, **kwargs)
    if case == "cancelled":
        store.cancel(rec.id)
    elif case == "withdrawn":
        store.withdraw(rec.id)
    elif case == "expired":
        store.expire(rec.id)
    elif case != "pending":
        store.answer(rec.id, "go", answer_kind="option")
    if case == "delivered":
        store.claim_delivery(rec.id)
    before = store.get(rec.id).to_dict()
    result = graph().invoke({"messages": [HumanMessage("Continue")]},
                            {"configurable": {"thread_id": THREAD}})
    assert not notes(result)
    assert store.get(rec.id).to_dict() == before


def test_read_failure_preserves_model_input(store, monkeypatch, caplog):
    def fail(thread_id):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(store, "undelivered_answers", fail)
    original = HumanMessage("Continue", id="input")
    result = graph().invoke({"messages": [original]}, {"configurable": {"thread_id": THREAD}})
    assert result["messages"][:-1] == [original]
    assert "Could not read decision context" in caplog.text


def test_delivery_claim_lost_after_read_does_not_duplicate_answer(store, monkeypatch):
    rec = store.answer(ask(store).id, "go", answer_kind="option")
    read = store.undelivered_answers

    def racing_read(thread_id):
        records = read(thread_id)
        store.claim_delivery(rec.id)
        return records

    monkeypatch.setattr(store, "undelivered_answers", racing_read)
    assert not notes(graph().invoke({"messages": [HumanMessage("Continue")]},
                                    {"configurable": {"thread_id": THREAD}}))
