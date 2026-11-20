"""Home entries (ADR 0135 D9, #5545): an owner notice is also written into Home's own
thread as one reference message, without starting a turn."""

from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from graph import home_entries, notice_hold
from graph.compaction_engine import HOME_ENTRY_KWARG, SYSTEM, human_kind
from graph.decisions import DecisionStore
from graph.plugins import chat_surface
from graph.plugins.host import HOST
from graph.thread_ids import resolve_thread_id
from runtime import home_session
from runtime.state import STATE

pytestmark = pytest.mark.asyncio
_FLAG_ENV = "PROTOAGENT_FLAG_HOME_CONVERSATION"


@pytest.fixture
def home(monkeypatch):
    """A real agent graph over an in-memory checkpointer, the Home flag on, and one notice
    adapter. ``model_calls`` counts model invocations: any would be a started turn."""
    for key in list(os.environ):
        if key.startswith("PROTOAGENT_FLAG_") or key == "PROTOAGENT_CHANNEL":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(_FLAG_ENV, "on")
    model_calls = []

    def replies():
        for i in range(3):
            model_calls.append(i)
            yield AIMessage(content=f"reply {i}")

    model = GenericFakeChatModel(messages=replies())
    graph = create_agent(model, tools=[], checkpointer=InMemorySaver())
    monkeypatch.setattr(STATE, "graph", graph)
    monkeypatch.setattr(STATE, "main_loop", None)
    sent, invoked = [], []
    monkeypatch.setattr(chat_surface, "_notifiers", {"test": sent.append})
    monkeypatch.setattr(HOST, "publish", lambda *_: None)
    monkeypatch.setattr(HOST, "invoke", lambda *a, **kw: invoked.append(a))
    monkeypatch.setattr(HOST, "config", lambda: SimpleNamespace(quiet_hours_enabled=False))
    monkeypatch.setattr(home_entries, "_thread_lock", None)
    monkeypatch.setattr(home_entries, "_pending", type(home_entries._pending)(maxlen=100))
    monkeypatch.setattr(home_entries, "_retry_task", None)
    config = {"configurable": {"thread_id": resolve_thread_id(None, home_session.home_session_id())}}

    async def messages():
        await _settle()
        return list((await graph.aget_state(config)).values.get("messages") or [])

    return SimpleNamespace(graph=graph, config=config, sent=sent, invoked=invoked, messages=messages,
                           model_calls=model_calls)


async def _settle():
    while home_entries._inflight:
        await asyncio.gather(*[asyncio.wrap_future(t) if not isinstance(t, asyncio.Task) else t
                               for t in list(home_entries._inflight)])
        await asyncio.sleep(0)


async def test_notify_notice_writes_one_home_message_and_starts_no_turn(home):
    notice_id = chat_surface.notify_chat_surfaces(
        "Watch deploy met: healthy\nall checks green", "https://console.example/app/",
        importance="high", source="watch",
    )
    [msg] = await home.messages()
    assert isinstance(msg, HumanMessage)
    assert msg.additional_kwargs[HOME_ENTRY_KWARG] == {
        "kind": "notify", "importance": "high", "source": "watch", "notice_id": notice_id,
    }
    assert msg.content.startswith(home_entries.HOME_ENTRY_PREFIX)
    assert "Watch deploy met: healthy" in msg.content and "all checks green" in msg.content
    assert "https://console.example/app/" in msg.content
    # A notice's text is never read as the operator's words.
    assert human_kind(msg) == SYSTEM
    # No turn: the model was never called and nothing was invoked.
    assert home.model_calls == [] and not home.invoked
    assert [n.notice_id for n in home.sent] == [notice_id]

    # A reply in Home then has the entry in context, ahead of the operator's message.
    out = await home.graph.ainvoke({"messages": [HumanMessage("what happened?")]}, home.config)
    assert [m.content for m in out["messages"][1:]] == ["what happened?", "reply 0"]
    assert home.model_calls == [0]
    assert out["messages"][0].id == msg.id


async def test_same_notice_id_replaces_instead_of_duplicating(home):
    for _ in range(2):
        assert await home_entries.write_home_entry("review", "normal", "Run done", "scheduler", notice_id="n1")
    assert [m.id for m in await home.messages()] == ["home-entry-n1"]


async def test_unattended_decision_writes_a_pointer_and_operator_chat_decision_writes_nothing(home, tmp_path):
    store = DecisionStore(tmp_path / "decisions.db")
    store.create("chat-abc", "Ship it?", ["yes", "no"], agent="Ava")
    assert await home.messages() == []
    assert len(home.sent) == 1  # the operator-chat decision still reaches the adapters

    rec = store.create("job:nightly", "Rotate the key?", ["rotate", "wait"], agent="Ava")
    [msg] = await home.messages()
    assert msg.additional_kwargs[HOME_ENTRY_KWARG] == {
        "kind": "question", "importance": "high", "source": "decision", "notice_id": f"decision-{rec.id}",
    }
    # A pointer: the question, not the options or the adapter instructions.
    assert "Ava needs a decision: Rotate the key?" in msg.content
    assert "- rotate" not in msg.content and "Open this decision" not in msg.content
    assert len(home.sent) == 2


async def test_quiet_hours_hold_the_adapters_but_not_the_home_entry(home, tmp_path, monkeypatch):
    cfg = SimpleNamespace(quiet_hours_enabled=True, quiet_hours_start="00:00",
                          quiet_hours_end="23:59", quiet_hours_timezone="UTC")
    monkeypatch.setattr(HOST, "config", lambda: cfg)
    monkeypatch.setattr(notice_hold.time, "time", lambda: 1_800_000_000.0)  # 03:20 UTC
    service = notice_hold.QuietHoursDelivery()
    service.store = notice_hold.NoticeHold(tmp_path / "hold.sqlite3")
    monkeypatch.setattr(notice_hold, "DELIVERY", service)

    notice_id = chat_surface.notify_chat_surfaces("nightly report", "/report")
    assert home.sent == [] and [text for _, text in service.store.rows()] == ["nightly report"]
    [msg] = await home.messages()
    assert msg.additional_kwargs[HOME_ENTRY_KWARG]["notice_id"] == notice_id


async def test_flag_off_writes_nothing(home, monkeypatch):
    monkeypatch.setenv(_FLAG_ENV, "off")
    chat_surface.notify_chat_surfaces("hello", "/x")
    assert await home_entries.write_home_entry("notify", "normal", "hello", "notice") is False
    assert await home.messages() == [] and not home_entries._inflight
    assert len(home.sent) == 1


async def test_write_failure_is_logged_and_does_not_stop_adapter_dispatch(home, monkeypatch, caplog):
    async def boom(*_a, **_kw):
        raise OSError("checkpoint store is down")

    monkeypatch.setattr(home.graph, "aupdate_state", boom, raising=False)
    with caplog.at_level(logging.WARNING, logger="protoagent.home"):
        notice_id = chat_surface.notify_chat_surfaces("hello", "/x")
        await _settle()
    assert [n.notice_id for n in home.sent] == [notice_id]
    assert "could not write the Home entry" in caplog.text

    # Even a failure to queue the entry leaves the notice path intact.
    monkeypatch.setattr(home_entries, "post_home_entry", lambda *a, **kw: 1 / 0)
    chat_surface.notify_chat_surfaces("again", "/x")
    assert len(home.sent) == 2


async def test_worker_thread_notice_writes_on_the_server_loop(home, monkeypatch):
    monkeypatch.setattr(STATE, "main_loop", asyncio.get_running_loop())
    locked = []

    class _Lock:
        def __init__(self, thread_id):
            self.thread_id = thread_id

        async def __aenter__(self):
            locked.append(self.thread_id)

        async def __aexit__(self, *_exc):
            return False

    home_entries.set_thread_lock(_Lock)
    await asyncio.to_thread(chat_surface.notify_chat_surfaces, "from a store thread", "/x")
    assert len(await home.messages()) == 1
    # The append is serialized with Home's turns on Home's own thread lock.
    assert locked == [home.config["configurable"]["thread_id"]]


async def test_home_paused_for_a_human_defers_the_entry_until_it_is_free(home, monkeypatch):
    paused = [True]
    real = home.graph.aget_state

    async def aget_state(config, *a, **kw):
        if paused[0]:
            return SimpleNamespace(tasks=[SimpleNamespace(result=None, interrupts=("form",))], values={})
        return await real(config, *a, **kw)

    monkeypatch.setattr(home.graph, "aget_state", aget_state, raising=False)
    monkeypatch.setattr(home_entries, "_RETRY_SECONDS", 0.01)
    assert await home_entries.write_home_entry("notify", "normal", "first", "notice", notice_id="a") is False
    assert await home_entries.write_home_entry("notify", "normal", "second", "notice", notice_id="b") is False
    paused[0] = False
    assert await home.messages() == []  # nothing was written over the pending form
    await asyncio.wait_for(home_entries._retry_task, 5)
    assert [m.id for m in await home.messages()] == ["home-entry-a", "home-entry-b"]
    assert not home_entries._pending
