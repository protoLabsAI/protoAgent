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
    reg = _Reg(scripts={"proto": ["a", "b", "c"], "reviewer": ["x", "y", "z"]})
    _wire(monkeypatch, reg, max_rounds=3, graph=graph)

    await sc._at_delegate_exchange("@proto @reviewer think it through", "m3")

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
    reg = _Reg(scripts={"proto": ["line 40", "pass"], "reviewer": ["pass"]})
    _wire(monkeypatch, reg, max_rounds=4)
    reply, _ = await sc._at_delegate_exchange("@proto @reviewer status?", "m5")
    assert reply == "**@proto** — line 40"


@pytest.mark.asyncio
async def test_everyone_passing_immediately_still_answers_the_operator(monkeypatch):
    reg = _Reg(scripts={"proto": ["pass"], "reviewer": ["(pass)"]})
    _wire(monkeypatch, reg, max_rounds=3)
    reply, _ = await sc._at_delegate_exchange("@proto @reviewer anything?", "m6")
    assert reply == "_Nobody had anything to add._"


@pytest.mark.asyncio
async def test_the_cap_ends_the_room_and_SAYS_so(monkeypatch):
    """A silent cap is the exact complaint this change exists to answer."""
    reg = _Reg(scripts={"proto": ["a", "b", "c"], "reviewer": ["x", "y", "z"]})
    _wire(monkeypatch, reg, max_rounds=2)

    reply, _ = await sc._at_delegate_exchange("@proto @reviewer keep going", "m7")

    assert len(reg.calls) == 4
    assert "2-round cap" in reply and "room.max_rounds" in reply


@pytest.mark.asyncio
async def test_a_failed_target_is_dropped_from_later_rounds(monkeypatch):
    reg = _Reg(
        names=("proto", "reviewer", "ana"),
        scripts={"proto": ["a", "b"], "reviewer": ["x"], "ana": ["c", "d"]},
        fails={"reviewer"},
    )
    _wire(monkeypatch, reg, max_rounds=2)

    _, outcomes = await sc._at_delegate_exchange("@proto @reviewer @ana status?", "m8")

    assert [c["name"] for c in reg.calls] == ["proto", "reviewer", "ana", "proto", "ana"]
    assert sum(1 for o in outcomes if not o["ok"]) == 1  # asked once, never retried


@pytest.mark.asyncio
async def test_losing_all_but_one_participant_ends_the_room_rather_than_billing_on(monkeypatch):
    """A member whose OWN wall-clock budget expired ("still running — the peer may still
    be working") is not re-dispatched: the room passes no resume handle, so a retry opens
    a second task on a peer already busy with the first and waits the same timeout again.
    What the room must not then do is spend its remaining rounds asking the survivor to
    react to a `(could not be reached: …)` envelope."""
    reg = _Reg(
        names=("reviewer", "fleetmate"),
        scripts={"reviewer": ["I'd blame auth", "still auth"], "fleetmate": ["x"]},
        fails={"fleetmate"},
    )
    _wire(monkeypatch, reg, max_rounds=3)

    reply, outcomes = await sc._at_delegate_exchange("@reviewer @fleetmate what broke?", "m8b")

    assert [c["name"] for c in reg.calls] == ["reviewer", "fleetmate"]  # no round 2
    assert [o["round"] for o in outcomes] == [1, 1]
    # The member is not silently declared dead — the adapter's own words reach the operator.
    assert "Delegate @fleetmate failed: connection refused" in reply


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
async def test_a_failed_address_is_not_told_to_widen_a_window_it_never_used(monkeypatch):
    """The window is computed BEFORE the dispatch and `truncated` rides the failure path
    unchanged, so the outcome of a refused connection is `{ok: False, truncated: True}`.
    Rendering the note there puts "raise `room.catchup_max_messages`" directly under
    "Delegate @proto failed: connection refused" — new, false, operator-facing advice on
    the DEFAULT path, since the truncation note is the one thing this change adds at
    `room.max_rounds: 1`."""
    from langchain_core.messages import HumanMessage

    graph = _Graph()
    graph.messages = [HumanMessage(content=f"m{i}") for i in range(60)]
    reg = _Reg(names=("proto",), fails={"proto"})
    cfg = _wire(monkeypatch, reg, max_rounds=1, graph=graph)
    cfg.room_catchup_max_messages = 5

    reply, outcomes = await sc._at_delegate_exchange("@proto what broke?", "t1b")

    assert outcomes[0]["truncated"] is True and outcomes[0]["ok"] is False
    assert reply == "Delegate @proto failed: connection refused"


@pytest.mark.asyncio
async def test_a_participant_that_only_passed_is_not_named_by_the_note(monkeypatch):
    """A pass gets no line in the reply body; it gets no note about its window either."""
    from langchain_core.messages import HumanMessage

    graph = _Graph()
    graph.messages = [HumanMessage(content=f"m{i}") for i in range(60)]
    reg = _Reg(scripts={"proto": ["line 40"], "reviewer": ["pass"]})
    cfg = _wire(monkeypatch, reg, max_rounds=2, graph=graph)
    cfg.room_catchup_max_messages = 5

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "t1c")

    clipped = {o["author"] for o in outcomes if o["truncated"]}
    assert clipped == {"proto", "reviewer"}  # both windows really did truncate
    assert "left out of the catch-up for @proto —" in reply  # …and only proto is named


@pytest.mark.asyncio
async def test_one_answer_out_of_two_addressees_keeps_its_byline(monkeypatch):
    """`spoken` is the flat list minus silences, so multi-round can shrink it to one for
    a room of two — and an unattributed body is the exact collapse the room exists to
    avoid. A2A and /v1 consumers get no `room_reply` frames, so the byline is all they
    have."""
    reg = _Reg(scripts={"proto": ["line 40", "pass"], "reviewer": ["pass", "pass"]})
    _wire(monkeypatch, reg, max_rounds=2)

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "t1d")

    assert sum(1 for o in outcomes if not o["silent"]) == 1
    assert reply == "**@proto** — line 40"


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


# --- the pass offer, and a room that must NOT settle ---------------------------


@pytest.mark.asyncio
async def test_the_offer_to_pass_reaches_the_delegate_only_above_one_round(monkeypatch):
    """The host lever (`RoundPlan.drop_silence`) has to reach the PROMPT, not just the
    outcome. If it stops at the outcome the room honours a token nobody was asked for,
    every round is full of prose, and the cap does all the stopping."""
    reg = _Reg(scripts={"proto": ["a", "pass"], "reviewer": ["b", "pass"]})
    _wire(monkeypatch, reg, max_rounds=2)
    await sc._at_delegate_exchange("@proto @reviewer status?", "p1")
    assert "reply with exactly `pass`" in reg.calls[0]["query"]

    single = _Reg(scripts={"proto": ["a"], "reviewer": ["b"]})
    _wire(monkeypatch, single, max_rounds=1)
    await sc._at_delegate_exchange("@proto @reviewer status?", "p2")
    assert "pass" not in single.calls[0]["query"].lower()

    # A solo cast is a one-round room whatever the knob says, so it is offered no pass
    # it could not act on — which is also what keeps `@one-agent do X` byte-identical.
    solo = _Reg(names=("proto",), scripts={"proto": ["a"]})
    _wire(monkeypatch, solo, max_rounds=5)
    await sc._at_delegate_exchange("@proto status?", "p2b")
    assert len(solo.calls) == 1 and "pass" not in solo.calls[0]["query"].lower()


@pytest.mark.asyncio
async def test_a_qualified_pass_is_an_answer_and_keeps_the_room_going(monkeypatch):
    """"Pass — but note X" is not silence. It carries the note the room needs, so it is
    recorded, attributed, and it does NOT settle the room: someone still has to answer
    it. The narrow token check is what makes that true."""
    graph = _Graph()
    reg = _Reg(scripts={"proto": ["Pass — but note the auth change landed", "pass"], "reviewer": ["pass", "pass"]})
    _wire(monkeypatch, reg, max_rounds=3, graph=graph)

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "p3")

    assert outcomes[0]["silent"] is False
    assert "note the auth change landed" in _room_texts(graph)[1]
    assert "note the auth change landed" in reply
    # Round 1 spoke, so round 2 runs; round 2 is all-silent and settles it there.
    assert [c["name"] for c in reg.calls] == ["proto", "reviewer", "proto", "reviewer"]
    assert "cap" not in reply


# --- the bounds are read through one guarded place -----------------------------


@pytest.mark.asyncio
async def test_a_hand_edited_junk_cap_falls_back_instead_of_500ing(monkeypatch):
    """`room.max_rounds: lots` is a YAML edit away, and this path runs inside an
    operator's `@`. A bare `int()` on it would surface as a failed chat turn, not as a
    config warning — so the cap is read through `mention_op.round_cap`."""
    reg = _Reg(names=("proto",), scripts={"proto": ["line 40"]})
    cfg = _wire(monkeypatch, reg, max_rounds=1)
    cfg.room_max_rounds = "lots"

    reply, _ = await sc._at_delegate_exchange("@proto status?", "p4")
    assert reply == "line 40" and len(reg.calls) == 1


@pytest.mark.asyncio
async def test_a_zeroed_cap_is_one_round_not_zero(monkeypatch):
    reg = _Reg(names=("proto",), scripts={"proto": ["line 40"]})
    cfg = _wire(monkeypatch, reg, max_rounds=1)
    cfg.room_max_rounds = 0

    reply, _ = await sc._at_delegate_exchange("@proto status?", "p5")
    assert reply == "line 40" and len(reg.calls) == 1


# --- the #3126 fall-through still holds with rounds on -------------------------


@pytest.mark.asyncio
async def test_an_all_unreachable_room_still_falls_through_to_the_lead(monkeypatch):
    """#3126: when EVERY addressed member is a stopped local one, the `@` returns None so
    the lead agent's consent/start path can offer to start it. The check reads round ONE
    (the only round that dispatches every target) — it used to read a length equality
    that merely happened to mean the same thing."""
    from plugins.delegates.adapters import KIND_UNREACHABLE

    class _Unreachable(RuntimeError):
        kind = KIND_UNREACHABLE

    class _Down(_Reg):
        async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
            self.calls.append({"name": name, "query": query})
            raise _Unreachable("connection refused")

    reg = _Down(names=("proto", "reviewer"))
    _wire(monkeypatch, reg, max_rounds=3)
    monkeypatch.setattr(
        "plugins.delegates.autostart.startable_member", lambda url: {"name": "member"}, raising=False
    )

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?", "p6")

    assert reply is None and outcomes is None
    assert len(reg.calls) == 2  # exhausted at round one — a dead delegate is not retried
