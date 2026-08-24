"""RoomCastMiddleware (#3049) — the lead is told who is in the chat, truthfully.

The cast derives from the thread's ``room`` stamps at each model call and rides as an
ephemeral system suffix. These pin the derivation rules (spoken = in; failed address =
out; operator/lead = out) and the injection mechanics (idempotent, absent on ordinary
chats, never a checkpointed message).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from graph.middleware.room_cast import RoomCastMiddleware, cast_line, participants


def _room(author, text="hi", **extra):
    return HumanMessage(content=text, additional_kwargs={"lc_source": "room", "room": {"from": author, **extra}})


# --- who counts -----------------------------------------------------------------


def test_speakers_in_first_spoken_order():
    msgs = [_room("operator", to="proto"), _room("proto"), _room("reviewer"), _room("proto")]
    assert participants(msgs) == ["proto", "reviewer"]


def test_the_operator_and_lead_are_not_listed():
    """Both are always present — listing them is noise, not information."""
    assert participants([_room("operator"), _room("assistant")]) == []


def test_a_failed_address_is_not_presence():
    """The delegate never received anything — it holds no context, and listing it
    would claim presence it doesn't have."""
    assert participants([_room("proto", failed=True)]) == []
    assert participants([_room("proto", failed=True), _room("proto")]) == ["proto"]


def test_an_ordinary_chat_has_no_cast():
    msgs = [HumanMessage(content="hello"), AIMessage(content="hi there")]
    assert participants(msgs) == []
    assert participants([]) == []
    assert participants(None) == []


# --- the injected line ----------------------------------------------------------


class _Sys:
    def __init__(self, content):
        self.content = content

    def model_copy(self, update):
        return _Sys(update["content"])


class _Req:
    def __init__(self, messages, system):
        self.messages = messages
        self.system_message = system

    def override(self, system_message):
        return _Req(self.messages, system_message)


def test_no_cast_means_no_touch():
    req = _Req([HumanMessage(content="hi")], _Sys("You are the agent."))
    assert RoomCastMiddleware()._transform(req) is req


def test_the_line_is_appended_as_the_last_system_block():
    req = _Req([_room("proto")], _Sys("You are the agent."))
    out = RoomCastMiddleware()._transform(req)
    blocks = out.system_message.content
    assert blocks[0] == {"type": "text", "text": "You are the agent."}
    assert blocks[-1]["text"].startswith("[room]") and "proto" in blocks[-1]["text"]


def test_reapplication_replaces_rather_than_stacks():
    """wrap_model_call runs every model call of a multi-round turn — the line must not
    pile up, and a stale cast must be replaced by the current one."""
    mw = RoomCastMiddleware()
    req = _Req([_room("proto")], _Sys("You are the agent."))
    once = mw._transform(req)
    req2 = _Req([_room("proto"), _room("reviewer")], once.system_message)
    twice = mw._transform(req2)
    marks = [b for b in twice.system_message.content if b["text"].startswith("[room]")]
    assert len(marks) == 1
    assert "reviewer" in marks[0]["text"]


def test_awareness_never_permission():
    """The wording is load-bearing: gating delegation was rejected as confusion by
    construction, so the line must SAY it doesn't gate."""
    line = cast_line(["proto"])
    assert "not permission" in line
    assert "whoever fits" in line


def test_the_identity_fence_is_explicit():
    """Live failure (2026-08-24): a delegate `proto` on an agent named `protoagent`
    made the lead answer a status question AS the delegate, in first person. Names
    WILL be similar in the wild, so the fence lives in the line, not in naming
    discipline: who the reader is, that the cast are other agents despite similar
    names, and never answer AS one."""
    line = cast_line(["proto"], lead_name="protoagent")
    assert "You are protoagent" in line
    assert "OTHER agents" in line
    assert "names look similar" in line
    assert "never answer AS them" in line


def test_the_fence_holds_without_a_configured_name():
    line = cast_line(["proto"])
    assert line.startswith("[room] You are the lead agent")
    assert "never answer AS them" in line


def test_the_envelope_reading_rule_is_stated():
    """The lead once read `<room-message from="operator" to="proto">` and concluded
    proto WAS the operator — say how to read the tag."""
    assert 'marks what X said' in cast_line(["proto"])


def test_no_system_message_is_left_alone():
    req = _Req([_room("proto")], None)
    assert RoomCastMiddleware()._transform(req) is req


# --- the registration-time shadow warning (delegates plugin) --------------------


def test_a_shadowing_delegate_name_warns(monkeypatch, caplog):
    import logging

    import runtime.state as rs
    from plugins.delegates import _warn_on_identity_shadow
    from plugins.delegates.registry import DelegateRegistry

    class _Cfg:
        identity_name = "protoagent"

    monkeypatch.setattr(rs.STATE, "graph_config", _Cfg(), raising=False)

    class _Reg(DelegateRegistry):
        def __init__(self, names):
            self._names = names

        def names(self):
            return self._names

    with caplog.at_level(logging.WARNING):
        _warn_on_identity_shadow(_Reg(["proto", "reviewer"]))
    warned = [r.message for r in caplog.records if "shadows" in r.message]
    assert len(warned) == 1 and "'proto'" in warned[0]  # reviewer is fine


def test_distinct_names_stay_quiet(monkeypatch, caplog):
    import logging

    import runtime.state as rs
    from plugins.delegates import _warn_on_identity_shadow
    from plugins.delegates.registry import DelegateRegistry

    class _Cfg:
        identity_name = "buzz"

    monkeypatch.setattr(rs.STATE, "graph_config", _Cfg(), raising=False)

    class _Reg(DelegateRegistry):
        def __init__(self):
            pass

        def names(self):
            return ["coder", "reviewer"]

    with caplog.at_level(logging.WARNING):
        _warn_on_identity_shadow(_Reg())
    assert not [r for r in caplog.records if "shadows" in r.message]
