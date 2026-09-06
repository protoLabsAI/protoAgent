"""Bounded multi-round rooms, driven end to end through `server/chat.py` (#3042).

`_at_delegate_exchange` used to loop the addressed targets exactly once. It now loops
ROUNDS, asking `graph.room_rounds` who speaks next. Two properties are load-bearing and
both are proved here:

* **`room.max_rounds: 1` is today.** The default is off, and off has to mean the shipped
  behavior down to the dispatch calls, the thread writes and the reply string — not
  "close enough".
* **Above 1 the cast re-runs, and it STOPS.** A `pass` is silence (no thread record, no
  attributed line, does not keep the room alive), an all-silent round settles, the cap
  is announced rather than silently applied, and a failed target is not re-dispatched.
"""

from __future__ import annotations

import importlib

import pytest

import runtime.state as rs
from graph.config import LangGraphConfig

sc = importlib.import_module("server.chat")


class _Delegate:
    type = "acp"
    url = "http://127.0.0.1:7801/a2a"


class _Snapshot:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _Graph:
    """A thread that accumulates, like the real checkpointer's `add_messages` reducer."""

    def __init__(self):
        self.messages: list = []

    async def aget_state(self, config):
        return _Snapshot(list(self.messages))

    async def aupdate_state(self, config, update, *, as_node=None):
        assert as_node is not None, "Ambiguous update, specify as_node"
        self.messages.extend(update["messages"])


class _Reg:
    """A roster whose replies can differ per ROUND — `scripts[name]` is read in order."""

    def __init__(self, names=("proto", "reviewer"), scripts=None, fails=()):
        self._names = list(names)
        self._scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self._fails = set(fails)
        self.calls: list[dict] = []

    def names(self):
        return list(self._names)

    def get(self, name):
        return _Delegate() if name in self._names else None

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
        self.calls.append({"name": name, "query": query})
        if name in self._fails:
            raise RuntimeError("connection refused")
        script = self._scripts.get(name)
        if not script:
            return f"{name} says hi"
        return script.pop(0) if len(script) > 1 else script[0]


def _wire(monkeypatch, reg, *, max_rounds=1, graph=None):
    cfg = LangGraphConfig()
    cfg.room_max_rounds = max_rounds
    monkeypatch.setattr(rs.STATE, "delegate_registry", reg, raising=False)
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)
    return cfg


def _room_texts(graph) -> list[str]:
    return [str(m.content) for m in graph.messages]


def _authors(graph) -> list[dict]:
    return [(m.additional_kwargs or {}).get("room") for m in graph.messages]


# --- room.max_rounds: 1 IS today ---------------------------------------------


@pytest.mark.asyncio
async def test_single_round_dispatches_each_target_exactly_once(monkeypatch):
    reg = _Reg(scripts={"proto": ["line 40", "second"], "reviewer": ["agreed", "second"]})
    _wire(monkeypatch, reg, max_rounds=1)

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "s1")

    assert [c["name"] for c in reg.calls] == ["proto", "reviewer"]
    assert [o["author"] for o in outcomes] == ["proto", "reviewer"]
    # The exact string this has always produced: attributed, in written order.
    assert reply == "**@proto** — line 40\n\n**@reviewer** — agreed"


@pytest.mark.asyncio
async def test_single_round_writes_exactly_the_same_thread_record(monkeypatch):
    graph = _Graph()
    reg = _Reg(names=("proto",), scripts={"proto": ["line 40"]})
    _wire(monkeypatch, reg, max_rounds=1, graph=graph)

    await sc._at_delegate_exchange("@proto the auth bug?", "s2")

    assert _authors(graph) == [{"from": "operator", "to": "proto"}, {"from": "proto"}]
    assert "the auth bug?" in _room_texts(graph)[0]
    assert "line 40" in _room_texts(graph)[1]


@pytest.mark.asyncio
async def test_single_round_still_records_a_literal_pass_verbatim(monkeypatch):
    """The one behavior a careless silence rule would break. At the default cap a `pass`
    is an ANSWER — quoted to the operator and written to the room, exactly as before."""
    graph = _Graph()
    reg = _Reg(names=("proto",), scripts={"proto": ["pass"]})
    _wire(monkeypatch, reg, max_rounds=1, graph=graph)

    reply, outcomes = await sc._at_delegate_exchange("@proto anything to add?", "s3")

    assert reply == "pass"
    assert outcomes[0]["silent"] is False
    assert _authors(graph) == [{"from": "operator", "to": "proto"}, {"from": "proto"}]


@pytest.mark.asyncio
async def test_a_host_with_no_room_config_at_all_behaves_as_today(monkeypatch):
    """`STATE.graph_config` is None on more paths than you'd think. The room must fall
    back to one round, not to zero rounds or an exception."""
    reg = _Reg(names=("proto",), scripts={"proto": ["line 40"]})
    monkeypatch.setattr(rs.STATE, "delegate_registry", reg, raising=False)
    monkeypatch.setattr(rs.STATE, "graph", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)

    reply, _ = await sc._at_delegate_exchange("@proto status?", "s4")
    assert reply == "line 40" and len(reg.calls) == 1


@pytest.mark.asyncio
async def test_a_single_addressee_reply_is_still_unattributed(monkeypatch):
    reg = _Reg(names=("proto",), scripts={"proto": ["line 40"]})
    _wire(monkeypatch, reg, max_rounds=1)
    reply, _ = await sc._at_delegate_exchange("@proto status?", "s5")
    assert reply == "line 40"


# --- more than one round ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cast_re_runs_in_the_same_order_each_round(monkeypatch):
    reg = _Reg(scripts={"proto": ["r1", "r2"], "reviewer": ["r1", "r2"]})
    _wire(monkeypatch, reg, max_rounds=2)

    _, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "m1")

    assert [c["name"] for c in reg.calls] == ["proto", "reviewer", "proto", "reviewer"]
    assert [o["round"] for o in outcomes] == [1, 1, 2, 2]


@pytest.mark.asyncio
async def test_round_two_catches_each_participant_up_on_round_one(monkeypatch):
    graph = _Graph()
    reg = _Reg(scripts={"proto": ["it's the migration", "still think so"], "reviewer": ["I'd blame auth", "ok"]})
    _wire(monkeypatch, reg, max_rounds=2, graph=graph)

    await sc._at_delegate_exchange("@proto @reviewer what broke?", "m2")

    round_two_proto = reg.calls[2]["query"]
    assert "[reviewer] I'd blame auth" in round_two_proto  # what the OTHER one just said
    assert "<room-message" not in round_two_proto  # envelopes never leak into a catch-up


@pytest.mark.asyncio
async def test_the_operator_message_is_not_re_written_every_round(monkeypatch):
    """It is one address, not N. Re-recording the operator's envelope per round would
    read, in every later catch-up, as the operator saying the same thing three times."""
    graph = _Graph()
    reg = _Reg(names=("proto",), scripts={"proto": ["a", "b", "c"]})
    _wire(monkeypatch, reg, max_rounds=3, graph=graph)

    await sc._at_delegate_exchange("@proto think it through", "m3")

    stamps = _authors(graph)
    assert sum(1 for s in stamps if s == {"from": "operator", "to": "proto"}) == 1
    assert sum(1 for s in stamps if s == {"from": "proto"}) == 3


@pytest.mark.asyncio
async def test_an_all_pass_round_settles_the_room_early(monkeypatch):
    graph = _Graph()
    reg = _Reg(scripts={"proto": ["line 40", "(pass)"], "reviewer": ["agreed", "pass"]})
    _wire(monkeypatch, reg, max_rounds=5, graph=graph)

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "m4")

    assert len(reg.calls) == 4  # round 3 never runs — the room settled
    # Silence is not a message: no thread record, and no attributed line.
    assert [s for s in _authors(graph) if s and s.get("from") == "proto"] == [{"from": "proto"}]
    assert reply == "**@proto** — line 40\n\n**@reviewer** — agreed"
    assert [o["silent"] for o in outcomes] == [False, False, True, True]


@pytest.mark.asyncio
async def test_a_settle_is_not_announced(monkeypatch):
    """The normal, good ending. A note on every settle would be chrome, not information."""
    reg = _Reg(names=("proto",), scripts={"proto": ["line 40", "pass"]})
    _wire(monkeypatch, reg, max_rounds=4)
    reply, _ = await sc._at_delegate_exchange("@proto status?", "m5")
    assert reply == "line 40"


@pytest.mark.asyncio
async def test_everyone_passing_immediately_still_answers_the_operator(monkeypatch):
    reg = _Reg(scripts={"proto": ["pass"], "reviewer": ["(pass)"]})
    _wire(monkeypatch, reg, max_rounds=3)
    reply, _ = await sc._at_delegate_exchange("@proto @reviewer anything?", "m6")
    assert reply == "_Nobody had anything to add._"


@pytest.mark.asyncio
async def test_the_cap_ends_the_room_and_SAYS_so(monkeypatch):
    """A silent cap is the exact complaint this change exists to answer."""
    reg = _Reg(names=("proto",), scripts={"proto": ["a", "b", "c"]})
    _wire(monkeypatch, reg, max_rounds=2)

    reply, _ = await sc._at_delegate_exchange("@proto keep going", "m7")

    assert len(reg.calls) == 2
    assert "2-round cap" in reply and "room.max_rounds" in reply


@pytest.mark.asyncio
async def test_a_failed_target_is_dropped_from_later_rounds(monkeypatch):
    reg = _Reg(scripts={"proto": ["a", "b"], "reviewer": ["x"]}, fails={"reviewer"})
    _wire(monkeypatch, reg, max_rounds=3)

    _, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "m8")

    assert [c["name"] for c in reg.calls] == ["proto", "reviewer", "proto", "proto"]
    assert sum(1 for o in outcomes if not o["ok"]) == 1  # asked once, never retried


@pytest.mark.asyncio
async def test_every_target_failing_ends_the_room_at_round_one(monkeypatch):
    reg = _Reg(scripts={}, fails={"proto", "reviewer"})
    _wire(monkeypatch, reg, max_rounds=4)

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "m9")

    assert len(reg.calls) == 2
    assert len(outcomes) == 2 and all(not o["ok"] for o in outcomes)
    assert "failed" in reply


# --- the truncation note ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_truncated_catchup_is_surfaced_to_the_operator(monkeypatch):
    """`truncated` has always been returned and has always gone only into the DELEGATE's
    preface. The operator saw a confident answer given on a partial view of the room and
    had no way to know — and no way to learn which knob to raise."""
    from langchain_core.messages import HumanMessage

    graph = _Graph()
    graph.messages = [HumanMessage(content=f"m{i}") for i in range(60)]
    reg = _Reg(names=("proto",), scripts={"proto": ["line 40"]})
    cfg = _wire(monkeypatch, reg, max_rounds=1, graph=graph)
    cfg.room_catchup_max_messages = 5

    reply, outcomes = await sc._at_delegate_exchange("@proto status?", "t1")

    assert outcomes[0]["truncated"] is True
    assert "left out of the catch-up for @proto" in reply
    assert "room.catchup_max_messages" in reply
    assert reply.startswith("line 40")  # the answer still leads


@pytest.mark.asyncio
async def test_an_untruncated_room_gets_no_note(monkeypatch):
    """Which is what keeps `room.max_rounds: 1` byte-identical for every ordinary room."""
    graph = _Graph()
    reg = _Reg(names=("proto",), scripts={"proto": ["line 40"]})
    _wire(monkeypatch, reg, max_rounds=1, graph=graph)
    reply, _ = await sc._at_delegate_exchange("@proto status?", "t2")
    assert reply == "line 40"


@pytest.mark.asyncio
async def test_the_configured_window_reaches_the_dispatch(monkeypatch):
    from langchain_core.messages import HumanMessage

    graph = _Graph()
    graph.messages = [HumanMessage(content=f"m{i}") for i in range(20)]
    reg = _Reg(names=("proto",), scripts={"proto": ["ok"]})
    cfg = _wire(monkeypatch, reg, max_rounds=1, graph=graph)
    cfg.room_catchup_max_messages = 3

    await sc._at_delegate_exchange("@proto status?", "t3")

    query = reg.calls[0]["query"]
    assert "[operator] m19" in query and "[operator] m10" not in query
