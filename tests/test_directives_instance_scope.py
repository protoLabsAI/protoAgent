"""Instance-wide standing orders (ADR 0135 D8/D13, #5542): an instance scope on the
existing directive store — rendered for every session, recorded only from an operator
chat turn, never reachable by widening a chat directive, and listed / withdrawn through
the directive routes."""

import importlib
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.directives import (
    INSTANCE_LEDGER,
    INSTANCE_TAG,
    DirectiveError,
    DirectiveStore,
    directives_command_reply,
    working_state_section,
)
from graph.projection import working_state_block
from observability.tracing import reset_trust_tier, set_trust_tier
from operator_api.chat_routes import register_chat_routes
from runtime.state import STATE
from tools.directive_tools import build_directive_tools

pytestmark = pytest.mark.platform_sensitive

OPERATOR = {"session_id": "chat-a", "operator_chat_turn": True}
SCHEDULED = {"session_id": "schedule:nightly", "operator_chat_turn": False}
ORDER = "Never email a customer without my sign-off."


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = DirectiveStore(tmp_path / "directives")
    monkeypatch.setattr(STATE, "directive_store", store)
    return store


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr("graph.directives._publish", lambda topic, data: seen.append(data))
    return seen


def call(name, state, **args):
    tool = next(t for t in build_directive_tools() if t.name == name)
    return tool.invoke({**args, "state": state})


def record_order(state=OPERATOR, **args):
    return call("record_directive", state, quote=ORDER, instance_wide=True, **args)


# ── r1: one order, every turn ────────────────────────────────────────────────────


def test_order_from_an_operator_turn_renders_for_other_and_scheduled_sessions(store, events):
    result = record_order()
    assert result.startswith("Recorded instance-wide directive dir-i")
    (order,) = store.list("", instance_wide=True)
    assert order.instance_wide and order.session_id == "chat-a" and order.quote == ORDER
    assert events == [{"session_id": "chat-a", "id": order.id, "instance_wide": True}]
    assert store.list("chat-a") == []  # not also a chat directive

    for session_id in ("chat-a", "chat-b", "schedule:nightly", "watch:inbox", ""):
        section = working_state_section(session_id)
        assert f"- {order.id} {INSTANCE_TAG}" in section and ORDER in section
        assert "every unattended turn" in section
    # The projection path a scheduled turn takes — its own state, no operator stamp.
    block = working_state_block(dict(SCHEDULED))
    assert ORDER in block and INSTANCE_TAG in block


def test_order_renders_ahead_of_the_sessions_own_directives(store):
    record_order()
    call("record_directive", {**OPERATOR, "session_id": "chat-b"}, quote="Hold the launch.")
    section = working_state_section("chat-b")
    assert section.index(ORDER) < section.index("Hold the launch.")
    assert section.count(INSTANCE_TAG + " (recorded") == 1
    assert "Hold the launch." not in working_state_section("chat-c")


def test_no_orders_leaves_the_section_unchanged(store):
    assert working_state_section("") == "" and working_state_section("chat-a") == ""
    call("record_directive", OPERATOR, quote="Hold the launch.")
    assert INSTANCE_TAG not in working_state_section("chat-a")


# ── r2: only an operator chat turn records one ───────────────────────────────────


@pytest.mark.parametrize(
    "state",
    [
        SCHEDULED,
        {"session_id": "watch:inbox"},
        {"session_id": "chat-a", "operator_chat_turn": "true"},
        {"session_id": "chat-a", "operator_chat_turn": True, "subagent_fence": ["record_directive"]},
    ],
)
def test_non_operator_turn_is_refused_with_a_clear_error(store, events, state):
    token = set_trust_tier("operator")  # an inherited contextvar must not authorize it
    try:
        result = record_order(state)
    finally:
        reset_trust_tier(token)
    assert result.startswith("Error: an instance-wide directive is recorded only from what the operator says")
    assert "this turn did not come from the operator" in result
    assert store.list("", instance_wide=True) == [] and store.list(state["session_id"]) == []
    assert events == [] and working_state_section("chat-b") == ""


def test_no_chat_is_refused(store):
    assert "No active chat" in record_order({"operator_chat_turn": True})
    assert store.list("", instance_wide=True) == []


# ── r3: reach is fixed at record time ────────────────────────────────────────────


def test_no_tool_call_widens_an_existing_chat_directive(store):
    call("record_directive", OPERATOR, quote="Hold the launch.")
    (chat,) = store.list("chat-a")
    tools = {t.name: t for t in build_directive_tools()}
    # Only record_directive (a NEW record) takes the flag; no tool takes an id and a reach.
    assert [n for n, t in tools.items() if "instance_wide" in t.args] == ["record_directive"]
    # The store's edit path addresses a ledger; it never moves a row between them.
    assert store.update("chat-a", chat.id, scope="launch").instance_wide is False
    with pytest.raises(DirectiveError):
        store.update("", chat.id, scope="launch", instance_wide=True)
    call("list_directives", OPERATOR, include_closed=True)
    for name in ("fulfil_directive", "revoke_directive"):
        for state in (OPERATOR, SCHEDULED | {"session_id": "chat-a"}):
            call(name, state, directive_id=chat.id, reason="done")
            assert store.list("", instance_wide=True) == []
            assert store.get("chat-a", chat.id).instance_wide is False
    assert working_state_section("chat-b") == ""


def test_a_stored_flag_in_a_chat_ledger_does_not_widen_it(store):
    chat = store.record("chat-a", "Hold the launch.")
    path = store._path("chat-a")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["directives"][0]["instance_wide"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get("chat-a", chat.id).instance_wide is False
    assert working_state_section("chat-b") == ""


def test_a_session_id_cannot_name_the_instance_ledger(store):
    record_order()
    (order,) = store.list("", instance_wide=True)
    assert store.list(INSTANCE_LEDGER) == [] and store.get(INSTANCE_LEDGER, order.id) is None
    with pytest.raises(DirectiveError, match="reserved"):
        store.record(INSTANCE_LEDGER, "x")
    with pytest.raises(DirectiveError):
        store.close(INSTANCE_LEDGER, order.id, status="revoked")
    with pytest.raises(DirectiveError):
        store.update(INSTANCE_LEDGER, order.id, scope="x")
    assert store.forget(INSTANCE_LEDGER) is False
    # Deleting the chat it was recorded in drops that chat's ledger, never the order.
    store.record("chat-a", "Hold the launch.")
    assert store.forget("chat-a") is True
    assert [d.id for d in store.in_force("", instance_wide=True)] == [order.id]


def test_an_unattended_turn_cannot_close_an_order(store):
    record_order()
    (order,) = store.list("", instance_wide=True)
    for name in ("fulfil_directive", "revoke_directive"):
        result = call(name, SCHEDULED, directive_id=order.id, reason="looks done")
        assert "instance-wide directive" in result and "did not come from the operator" in result
    assert store.get("", order.id, instance_wide=True).in_force
    listed = call("list_directives", SCHEDULED)
    assert order.id in listed and "Instance-wide, in force" in listed
    # An operator chat turn in ANY chat can — the order is not tied to where it was given.
    result = call("revoke_directive", {**OPERATOR, "session_id": "chat-b"}, directive_id=order.id, reason="they said stop")
    assert result == f"Directive {order.id} is now revoked: they said stop"
    assert working_state_section("schedule:nightly") == ""


# ── r4: the directive routes ─────────────────────────────────────────────────────


@pytest.fixture
def api(store, events, monkeypatch):
    note = AsyncMock(return_value=True)
    monkeypatch.setattr(importlib.import_module("server.chat"), "record_directive_withdrawal", note)
    monkeypatch.setattr(STATE, "a2a_task_engine", None, raising=False)
    app = FastAPI()
    register_chat_routes(app, ui="none")
    with TestClient(app) as client:
        yield client, note


def test_routes_list_patch_and_withdraw_an_order(api, store):
    client, note = api
    record_order()
    call("record_directive", OPERATOR, quote="Hold the launch.")
    (order,) = store.list("", instance_wide=True)

    listed = client.get("/api/directives").json()
    assert listed == {"directives": [order.to_dict()], "in_force": 1}
    assert listed["directives"][0]["instance_wide"] is True
    assert client.get("/api/directives", params={"status": "bogus"}).status_code == 400
    # A chat's own list is unchanged: its directives only.
    assert [d["quote"] for d in client.get("/api/chat/sessions/chat-a/directives").json()["directives"]] == [
        "Hold the launch."
    ]

    url = f"/api/directives/{order.id}"
    patched = client.patch(url, params={"instance_wide": "true"}, json={"scope": "customers"}).json()["directive"]
    assert patched == {**order.to_dict(), "scope": "customers"}
    # The id is scoped to one ledger, and the request must name exactly one.
    assert client.patch(url, params={"session_id": "chat-a"}, json={"scope": "x"}).status_code == 404
    assert client.patch(url, json={"scope": "x"}).status_code == 422
    assert client.post(f"{url}/withdraw").status_code == 422
    both = {"session_id": "chat-a", "instance_wide": "true"}
    assert client.post(f"{url}/withdraw", params=both).status_code == 422
    assert client.post(f"{url}/withdraw", params={"session_id": "chat-a"}).status_code == 404
    note.assert_not_awaited()

    response = client.post(f"{url}/withdraw", params={"instance_wide": "true", "reason": "policy changed"})
    assert response.status_code == 200
    closed = response.json()["directive"]
    assert (closed["status"], closed["closed_by"], closed["close_reason"]) == ("revoked", "operator", "policy changed")
    note.assert_awaited_once()
    assert note.await_args.args[0] == "chat-a" and note.await_args.args[2] == "policy changed"
    # Withdrawn: no session renders it any more; the chat's own directive still does.
    assert working_state_section("chat-b") == "" and working_state_section("schedule:nightly") == ""
    assert ORDER not in working_state_section("chat-a")
    assert client.get("/api/directives").json()["in_force"] == 0
    assert client.get("/api/directives", params={"status": "revoked"}).json()["directives"] == [closed]
    assert client.post(f"{url}/withdraw", params={"instance_wide": "true"}).status_code == 409
    assert client.patch(url, params={"instance_wide": "true"}, json={"scope": "x"}).status_code == 409
    assert client.post("/api/directives/dir-nope/withdraw", params={"instance_wide": "true"}).status_code == 404


def test_the_create_route_cannot_record_an_order(api, store):
    client, _ = api
    for extra in ({"instance_wide": True}, {"session_id": INSTANCE_LEDGER}):
        body = {"session_id": "chat-a", "quote": ORDER, **extra}
        assert client.post("/api/directives", json=body).status_code == 422
    assert store.list("", instance_wide=True) == []


def test_withdraw_skips_the_note_when_the_origin_chat_is_gone(api, store, monkeypatch):
    client, note = api
    record_order()
    (order,) = store.list("", instance_wide=True)
    monkeypatch.setattr("operator_api.chat_routes.session_summary", AsyncMock(return_value=None))
    url = f"/api/directives/{order.id}/withdraw"
    assert client.post(url, params={"instance_wide": "true"}).status_code == 200
    note.assert_not_awaited()
    assert not store.get("", order.id, instance_wide=True).in_force


# ── shared paths one hop away ────────────────────────────────────────────────────


def test_directives_command_lists_and_revokes_an_order_from_any_chat(store):
    record_order()
    (order,) = store.list("", instance_wide=True)
    listing = directives_command_reply("chat-b", "")
    assert "Instance-wide, in force" in listing and order.id in listing
    assert "Only the operator" in directives_command_reply("chat-b", f"revoke {order.id}")
    token = set_trust_tier("operator")
    try:
        reply = directives_command_reply("chat-b", f"revoke {order.id} no longer needed")
    finally:
        reset_trust_tier(token)
    assert reply.startswith(f"Revoked {order.id}. It is no longer in force in any chat")
    assert store.get("", order.id, instance_wide=True).close_reason == "no longer needed"


def test_linked_order_fulfils_itself_and_task_autoclose_finds_it(store, events):
    from tasks.autoclose import _default_get_directive

    record_order(links=["acme/widgets#7"])
    (order,) = store.linked_in_force()
    assert order.instance_wide and _default_get_directive("chat-a", order.id).id == order.id
    (touched,) = store.resolve_link("acme/widgets#7", "closed")
    assert touched.status == "fulfilled" and touched.closed_by == "system"
    assert events[-1] == {"session_id": "chat-a", "id": order.id, "instance_wide": True}
    assert _default_get_directive("chat-a", order.id).status == "fulfilled"
    assert working_state_section("chat-b") == ""
