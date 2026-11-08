"""The operator-withdrawal transcript note (ADR 0127 D3, #4861).

When an operator withdraws a directive from the console or the API, the ``DELETE`` appends
one operator-originated note to the chat thread — ``You withdrew: "<first line>"`` — so a
later compaction carries it next to the original approval instead of leaving the approval
standing word for word with nothing after it. A ``/directives revoke`` typed in chat already
leaves the operator's own message, so that path gets no second note.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import runtime.state as rs
from graph.compaction_engine import (
    DIRECTIVE_WITHDRAWN_KWARG,
    OPERATOR,
    SENT_AT_KWARG,
    collect_operator_messages,
    human_kind,
    operator_messages_block,
    sent_at_kwargs,
)
from graph.directives import (
    MAX_NOTE_CHARS,
    STATUS_FULFILLED,
    STATUS_REVOKED,
    DirectiveStore,
    directives_command_reply,
    withdrawal_note,
)

APPROVAL = (
    "once #4801 and #4808 part 1 have both merged, cut the release … if the review panel gives "
    "the release PR a WARN or FAIL, come back to me instead of merging."
)


class FakeGraph:
    """Records the out-of-band ``aupdate_state`` writes the note makes (or raises on them)."""

    def __init__(self, *, fail: bool = False):
        self.updates: list[tuple[dict, dict]] = []
        self.fail = fail

    async def aupdate_state(self, config, values):
        if self.fail:
            raise RuntimeError("checkpointer is down")
        self.updates.append((config, values))
        return config


@pytest.fixture
def store(tmp_path, monkeypatch) -> DirectiveStore:
    s = DirectiveStore(tmp_path / "directives")
    monkeypatch.setattr(rs.STATE, "directive_store", s, raising=False)
    return s


def _client(monkeypatch, graph: FakeGraph) -> TestClient:
    import operator_api.chat_routes as cr

    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


@pytest.fixture
def operator_tier():
    from observability.tracing import reset_trust_tier, set_trust_tier

    token = set_trust_tier("operator")
    yield
    reset_trust_tier(token)


# ── the note text ───────────────────────────────────────────────────────────────────


def test_note_one_line_no_reason():
    assert withdrawal_note("ship it once CI is green") == 'You withdrew: "ship it once CI is green"'


def test_note_takes_only_the_first_line_of_a_multiline_quote():
    quote = "cut the release\nbut stop on a WARN\nand ping me"
    assert withdrawal_note(quote) == 'You withdrew: "cut the release"'


def test_note_appends_the_reason_with_an_em_dash():
    assert withdrawal_note("hold the release", "plan changed") == 'You withdrew: "hold the release" — plan changed'


def test_note_omits_the_suffix_for_a_blank_or_whitespace_reason():
    assert withdrawal_note("hold the release", "") == 'You withdrew: "hold the release"'
    assert withdrawal_note("hold the release", "   ") == 'You withdrew: "hold the release"'
    # The store's default close reason is never passed here, so it can never appear.
    assert "revoked by the operator" not in withdrawal_note("hold the release", "")


def test_note_clips_a_very_long_first_line_and_reason():
    note = withdrawal_note("x" * (MAX_NOTE_CHARS + 500), "y" * (MAX_NOTE_CHARS + 500))
    assert note.startswith('You withdrew: "' + "x" * 50)
    assert note.count("…") == 2  # head clipped, reason clipped
    assert len(note) < 2 * (MAX_NOTE_CHARS + 40)


# ── the DELETE route appends the note ─────────────────────────────────────────────────


def test_delete_with_reason_appends_one_operator_note_with_the_marker(store, monkeypatch):
    graph = FakeGraph()
    c = _client(monkeypatch, graph)
    d = store.record("chat-a", APPROVAL)

    r = c.delete(f"/api/chat/sessions/chat-a/directives/{d.id}?reason=plan changed")
    assert r.status_code == 200 and r.json()["directive"]["status"] == STATUS_REVOKED

    assert len(graph.updates) == 1
    config, values = graph.updates[0]
    assert config["configurable"]["thread_id"] == "a2a:chat-a"
    [msg] = values["messages"]
    assert isinstance(msg, HumanMessage)
    assert msg.content == f'You withdrew: "{APPROVAL}" — plan changed'
    assert msg.additional_kwargs[DIRECTIVE_WITHDRAWN_KWARG] == {"id": d.id}
    assert SENT_AT_KWARG in msg.additional_kwargs
    assert human_kind(msg) == OPERATOR


def test_delete_without_reason_appends_a_note_without_the_default(store, monkeypatch):
    graph = FakeGraph()
    c = _client(monkeypatch, graph)
    d = store.record("chat-a", APPROVAL)

    assert c.delete(f"/api/chat/sessions/chat-a/directives/{d.id}").status_code == 200
    [(_, values)] = graph.updates
    [msg] = values["messages"]
    assert msg.content == f'You withdrew: "{APPROVAL}"'
    assert "revoked by the operator" not in msg.content


def test_a_404_appends_nothing(store, monkeypatch):
    graph = FakeGraph()
    c = _client(monkeypatch, graph)
    d = store.record("chat-a", APPROVAL)

    assert c.delete(f"/api/chat/sessions/chat-b/directives/{d.id}").status_code == 404
    assert c.delete("/api/chat/sessions/chat-a/directives/dir-nope").status_code == 404
    assert graph.updates == []


def test_a_409_appends_nothing(store, monkeypatch):
    graph = FakeGraph()
    c = _client(monkeypatch, graph)
    d = store.record("chat-a", APPROVAL)
    store.close("chat-a", d.id, status=STATUS_FULFILLED, reason="done")

    assert c.delete(f"/api/chat/sessions/chat-a/directives/{d.id}").status_code == 409
    assert graph.updates == []


def test_slash_revoke_appends_no_note(store, operator_tier, monkeypatch):
    # /directives revoke leaves the operator's own typed message, so it gets no second note.
    graph = FakeGraph()
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    from graph import steering

    d = store.record("chat-a", APPROVAL)
    reply = directives_command_reply("chat-a", f"revoke {d.id} we're not releasing today")

    assert reply.startswith(f"Revoked {d.id}")
    assert store.get("chat-a", d.id).status == STATUS_REVOKED
    assert graph.updates == []
    assert steering.drain("chat-a") == []


def test_a_failing_append_still_returns_200_with_the_closed_directive(store, monkeypatch):
    graph = FakeGraph(fail=True)
    c = _client(monkeypatch, graph)
    d = store.record("chat-a", APPROVAL)

    r = c.delete(f"/api/chat/sessions/chat-a/directives/{d.id}?reason=plan changed")
    assert r.status_code == 200
    body = r.json()["directive"]
    assert body["status"] == STATUS_REVOKED and body["closed_by"] == "operator"
    assert graph.updates == []  # the append raised, was logged, and never surfaced


def test_no_graph_skips_quietly(store, monkeypatch):
    c = _client(monkeypatch, None)  # STATE.graph is None
    d = store.record("chat-a", APPROVAL)
    assert c.delete(f"/api/chat/sessions/chat-a/directives/{d.id}").status_code == 200


def test_an_active_turn_queues_on_the_steering_queue_not_aupdate_state(store, monkeypatch):
    graph = FakeGraph()
    c = _client(monkeypatch, graph)
    from graph import steering

    steering.drain("chat-a")  # start clean
    monkeypatch.setattr("runtime.turn_activity.is_active", lambda sid: True)
    d = store.record("chat-a", APPROVAL)

    assert c.delete(f"/api/chat/sessions/chat-a/directives/{d.id}?reason=plan changed").status_code == 200
    assert graph.updates == []  # did NOT block the DELETE on the running turn's thread lock
    queued = steering.drain("chat-a")
    assert [item["text"] for item in queued] == [f'You withdrew: "{APPROVAL}" — plan changed']


# ── the key test: compaction carries both the approval and the withdrawal ─────────────


def test_compaction_carries_the_approval_and_the_withdrawal_in_order(store, monkeypatch):
    graph = FakeGraph()
    c = _client(monkeypatch, graph)
    d = store.record("chat-a", APPROVAL)

    # The withdrawal note is produced by the real route path.
    assert c.delete(f"/api/chat/sessions/chat-a/directives/{d.id}?reason=plan changed").status_code == 200
    withdrawal = graph.updates[0][1]["messages"][0]

    # A realistic compacted span: the operator's approval, the agent working, then the note.
    approval = HumanMessage(content=APPROVAL, additional_kwargs=sent_at_kwargs())
    thread = [
        approval,
        AIMessage(content="Understood — I'll cut it once both merge."),
        ToolMessage(content="release PR opened", tool_call_id="tc-1", name="open_pr"),
        withdrawal,
    ]

    earlier, entries = collect_operator_messages(thread)
    assert earlier == 0
    assert [e.text for e in entries] == [APPROVAL, withdrawal.content]

    block = operator_messages_block(thread)
    assert APPROVAL in block
    assert 'You withdrew: "' in block
    # In order: the approval entry first, the withdrawal entry after it.
    assert block.index(APPROVAL) < block.index('You withdrew: "')
