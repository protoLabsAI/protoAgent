"""The non-streaming driver's trace output is the reply it returned, on every path (#3695).

#3693 fixed this for the streaming driver. The collected path (``/v1``, ``/api/chat``,
``HOST.invoke()``) returns from a dozen places inside its ``trace_session``: the
``@delegate`` and slash-command short-circuits, HITL parks and error bubbles, none of
which is a tool-call-free model reply. So its ``chat`` traces had input and no output.
These drive the real ``_chat_langgraph_impl`` against a real ``trace_session``.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

import server
from observability import tracing

# `server.chat` the attribute is the re-exported `chat` function; take the module.
chat = importlib.import_module("server.chat")


class _NoSkills:
    def user_facing_skills(self):
        return []


@pytest.fixture
def session_span(monkeypatch):
    fake = MagicMock()
    span = MagicMock()
    span.trace_id = "c" * 32
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(server.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(server.STATE, "plugin_chat_commands", {}, raising=False)
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(server.STATE, "skills_index", _NoSkills(), raising=False)
    return span


def _outputs(span) -> list:
    return [c.kwargs["output"] for c in span.update.call_args_list if "output" in c.kwargs]


async def test_a_slash_command_short_circuit_records_its_reply(session_span):
    out = await chat._chat_langgraph_impl("/foobar some args", "sess-1")

    assert _outputs(session_span) == [out[-1]["content"]]
    assert "Unknown command /foobar" in _outputs(session_span)[0]


async def test_a_plugin_command_reply_is_recorded_redacted(session_span, monkeypatch):
    async def issue(rest, session_id):  # noqa: ARG001 — signature parity
        return "filed it. token sk-" + "A" * 40

    monkeypatch.setattr(server.STATE, "plugin_chat_commands", {"issue": issue}, raising=False)

    await chat._chat_langgraph_impl("/issue 42", "sess-2")

    (out,) = _outputs(session_span)
    assert out.startswith("filed it.")
    assert "A" * 40 not in out


async def test_an_incognito_turn_records_no_output(session_span):
    await chat._chat_langgraph_impl("/foobar", "sess-3", incognito=True)

    assert _outputs(session_span) == []


def test_the_last_assistant_message_is_the_output(monkeypatch):
    seen: list = []
    monkeypatch.setattr(chat, "_set_trace_output", seen.append)

    chat._trace_reply_output(
        [
            {"role": "assistant", "content": "first"},
            {"role": "tool", "content": "x"},
            {"role": "assistant", "content": "last"},
        ]
    )
    chat._trace_reply_output("not a reply list")
    chat._trace_reply_output([{"role": "user", "content": "no assistant"}])

    assert seen == ["last"]
