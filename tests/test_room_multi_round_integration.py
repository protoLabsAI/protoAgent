"""A two-round room against the REAL compiled graph + checkpointer (#3042).

`tests/test_room_multi_round.py` proves the driver's policy against a thread that is a
list. This proves the thing a list cannot: that N rounds of `aupdate_state(…,
as_node=START)` actually land on the real `ProtoAgentState` schema through a real
checkpointer, and — the point of the whole room — that the NEXT ordinary turn's LEAD
agent reads every round back.

That last hop is why this file exists rather than more unit tests. The operator's next
bare message goes to the lead, not to the delegates; a round that never reaches the lead
is a side channel, and the failure is silent. A fake graph hid a real `as_node` bug on
exactly this code path once already.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

import runtime.state as rs
from graph.config import LangGraphConfig

sc = importlib.import_module("server.chat")


class _LeadFake(GenericFakeChatModel):
    """A lead model that records the history each turn actually handed it."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        type(self).seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class _Delegate:
    type = "acp"
    url = "http://127.0.0.1:7801/a2a"


class _Reg:
    """Two participants with a scripted line per round."""

    def __init__(self, scripts):
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list[dict] = []

    def names(self):
        return list(self._scripts)

    def get(self, name):
        return _Delegate() if name in self._scripts else None

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
        self.calls.append({"name": name, "query": query})
        script = self._scripts[name]
        return script.pop(0) if len(script) > 1 else script[0]


@pytest.fixture
def wired(monkeypatch):
    """The real graph, a recording fake lead model, and a two-round room."""
    _LeadFake.seen = []
    fake = _LeadFake(messages=iter([AIMessage(content="both of them think it's the migration")]))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        graph = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())

    cfg = LangGraphConfig()
    cfg.room_max_rounds = 2
    reg = _Reg({"proto": ["it's the migration", "yes — the 0042 one"], "reviewer": ["I'd blame auth", "agreed, migration"]})
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "delegate_registry", reg, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)
    return graph, reg


async def _room_messages(graph, session_id: str):
    tid = sc._resolve_thread_id(None, session_id)
    snapshot = await graph.aget_state({"configurable": {"thread_id": tid}})
    messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])
    return [m for m in messages if (m.additional_kwargs or {}).get("lc_source") == "room"]


@pytest.mark.asyncio
async def test_both_rounds_land_on_the_real_thread(wired):
    graph, reg = wired

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer what broke?", "round-1")

    assert [o["round"] for o in outcomes] == [1, 1, 2, 2]
    stamps = [m.additional_kwargs["room"] for m in await _room_messages(graph, "round-1")]
    # One address (the operator asked once) followed by two rounds of two replies.
    assert stamps == [
        {"from": "operator", "to": "proto"},
        {"from": "proto"},
        {"from": "operator", "to": "reviewer"},
        {"from": "reviewer"},
        {"from": "proto"},
        {"from": "reviewer"},
    ]
    assert "**@proto**" in reply and "**@reviewer**" in reply


@pytest.mark.asyncio
async def test_round_two_reads_round_one_off_the_real_checkpointer(wired):
    _, reg = wired

    await sc._at_delegate_exchange("@proto @reviewer what broke?", "round-2")

    round_two_proto = reg.calls[2]["query"]
    assert "[reviewer] I'd blame auth" in round_two_proto
    assert "<room-message" not in round_two_proto  # the envelope never re-wraps
    # And it is not told what it already said.
    assert "[proto] it's the migration" not in round_two_proto


@pytest.mark.asyncio
async def test_a_later_ordinary_turn_hands_the_lead_agent_every_round(wired):
    """The whole reason the thread IS the room: the operator's next bare message goes to
    the LEAD, and it must be able to answer "what did they decide?" having seen all of
    it — round two included, not just the opening exchange."""
    graph, _ = wired

    await sc._at_delegate_exchange("@proto @reviewer what broke?", "round-3")
    out = await sc.chat("so what did they decide?", "round-3")

    assert out[0]["content"] == "both of them think it's the migration"
    handed = "\n".join(str(m.content) for m in _LeadFake.seen[-1])
    for said in ("it's the migration", "I'd blame auth", "yes — the 0042 one", "agreed, migration"):
        assert said in handed, said
    # Authorship survives to the lead, structurally: it must never read a participant's
    # words as its own prior output.
    assert 'from="proto"' in handed and 'from="reviewer"' in handed


@pytest.mark.asyncio
async def test_the_cast_line_names_both_participants_to_the_lead(wired):
    """RoomCastMiddleware derives the cast from the thread's own `room` stamps — so a
    multi-round exchange has to leave stamps the middleware can read."""
    from graph.middleware.room_cast import participants

    graph, _ = wired
    await sc._at_delegate_exchange("@proto @reviewer what broke?", "round-4")

    tid = sc._resolve_thread_id(None, "round-4")
    snapshot = await graph.aget_state({"configurable": {"thread_id": tid}})
    assert participants(snapshot.values["messages"]) == ["proto", "reviewer"]
