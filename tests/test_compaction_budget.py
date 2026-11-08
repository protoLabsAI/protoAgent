"""Compaction budgets follow the running notes and the summarizer's connection."""

import asyncio
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from graph import compaction_engine as engine
from graph.compaction_op import _default_budget_chars, compact_thread


@pytest.mark.parametrize("asynchronous", [False, True])
def test_growing_notes_replan_without_losing_transcript(asynchronous):
    budget = 24_000
    transcript = "".join(f"{i:05d}" for i in range(11_000))
    messages = [AIMessage(content=transcript)]
    prompts = []

    def summarize(prompt):
        prompts.append(prompt)
        assert len(prompt) <= budget
        return "notes " + "n" * (8_000 + 600 * len(prompts))

    if asynchronous:

        async def async_summarize(prompt):
            return summarize(prompt)

        asyncio.run(engine.asummarize_span(messages, async_summarize, budget_chars=budget))
    else:
        engine.summarize_span(messages, summarize, budget_chars=budget)

    assert len(prompts) > 2
    chunks = [p.split("<transcript>\n", 1)[1].split("\n</transcript>", 1)[0] for p in prompts]
    assert "".join(re.sub(r"\D", "", chunk) for chunk in chunks) == transcript
    assert all("YOU (assistant) (continued): " in chunk for chunk in chunks[1:])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("seeded", [False, True])
def test_notes_leaving_less_than_minimum_chunk_raise(asynchronous, seeded):
    budget = 16_000
    notes = "n" * 14_000
    messages = [AIMessage(content="x" * 30_000)]
    if seeded:
        messages.insert(0, engine.summary_message(notes))
    calls = []

    def summarize(prompt):
        calls.append(prompt)
        return notes

    with pytest.raises(engine.CompactionSummaryError):
        if asynchronous:

            async def async_summarize(prompt):
                return summarize(prompt)

            asyncio.run(engine.asummarize_span(messages, async_summarize, budget_chars=budget))
        else:
            engine.summarize_span(messages, summarize, budget_chars=budget)
    assert len(calls) == (0 if seeded else 1)
    assert all(len(prompt) <= budget for prompt in calls)


def test_budget_uses_compaction_connection_window(monkeypatch):
    from graph import model_window
    from graph.config import LangGraphConfig, Provider

    config = LangGraphConfig(
        model_name="main:shared-model",
        compaction_model="compact:shared-model",
        providers=[
            Provider(id="main", type="openai-compat", base_url="https://main.example/v1"),
            Provider(id="compact", type="openai-compat", base_url="https://compact.example/v1"),
        ],
    )
    routes = []

    def window(route, model):
        routes.append((route.base_url, model))
        return 12_000 if route.base_url == "https://compact.example/v1" else 200_000

    monkeypatch.setattr(model_window, "_window_on_route", window)
    assert _default_budget_chars(config) == engine.summarizer_budget_chars(12_000)
    assert routes == [("https://compact.example/v1", "shared-model")]


@pytest.mark.parametrize("empty_answer", [False, True])
def test_force_later_chunk_failure_removes_span_and_keeps_tail(empty_answer):
    messages = [
        HumanMessage(content="keep this instruction", id="h1"),
        AIMessage(content="x" * 30_000, id="a1"),
        HumanMessage(content="latest question", id="h2"),
        AIMessage(content="latest answer", id="a2"),
    ]
    updates = []

    async def get_state(config):
        return SimpleNamespace(values={"messages": messages})

    async def update_state(config, update):
        updates.append(update)

    calls = []

    async def summarize(prompt, config):
        calls.append(prompt)
        if len(calls) == 2:
            if empty_answer:
                return ""
            raise RuntimeError("second chunk failed")
        return "partial notes must not become the summary"

    result = asyncio.run(
        compact_thread(
            SimpleNamespace(aget_state=get_state, aupdate_state=update_state),
            object(),
            None,
            SimpleNamespace(compaction_keep_messages=2),
            "a2a:s",
            "s",
            history_store=None,
            summarizer=summarize,
            force=True,
            budget_chars=16_000,
        )
    )
    assert len(calls) == 2
    assert not result["refused"]
    assert result["removed"] == 2 and result["kept"] == 2
    assert len(updates) == 1
    out = updates[0]["messages"]
    assert isinstance(out[0], RemoveMessage) and out[0].id == REMOVE_ALL_MESSAGES
    assert "No summary is available" in out[1].content
    assert "partial notes" not in out[1].content
    assert "keep this instruction" in out[1].content
    assert out[2:] == messages[2:]
