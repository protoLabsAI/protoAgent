"""User-facing skill slash commands (ADR 0052 — /<skill> in chat).

Unlike /<workflow> and /<subagent> (which short-circuit the turn and run a
worker), a /<skill> command REWRITES the message to inject the skill's procedure
as a directive and falls through to the normal lead-agent turn. These tests
cover the parser + directive builder; the fall-through itself is exercised by
the streaming integration tests.

Also covers the unknown-command guard (#2893): a message that LOOKS like a slash
command but matches no registered kind short-circuits both turn paths with a
hint instead of running a normal agent turn on the raw `/foobar` text.
"""

from __future__ import annotations

import pytest

import server


class _SkillsIdx:
    """Minimal STATE.skills_index lookalike exposing user_facing_skills()."""

    def __init__(self, skills):
        self._skills = skills

    def user_facing_skills(self):
        return self._skills


_RESEARCH = {
    "name": "web-research",
    "description": "Research on the web.",
    "prompt_template": "Plan, search, read, cite.",
    "slash": "research",
}


def test_known_skill_parses_to_skill_and_args(monkeypatch):
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(server.STATE, "skills_index", _SkillsIdx([_RESEARCH]), raising=False)
    parsed = server._parse_skill_command("/research compare uv vs poetry")
    assert parsed is not None
    skill, args = parsed
    assert skill["name"] == "web-research"
    assert args == "compare uv vs poetry"


def test_bare_skill_yields_empty_args(monkeypatch):
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(server.STATE, "skills_index", _SkillsIdx([_RESEARCH]), raising=False)
    parsed = server._parse_skill_command("/research")
    assert parsed is not None and parsed[1] == ""


def test_unknown_and_non_command_return_none(monkeypatch):
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(server.STATE, "skills_index", _SkillsIdx([_RESEARCH]), raising=False)
    assert server._parse_skill_command("/not-a-skill hi") is None
    assert server._parse_skill_command("just chatting") is None
    assert server._parse_skill_command("   ") is None


def test_blank_slash_matches_slugified_name(monkeypatch):
    """A user-facing skill with no explicit slash is reachable via its name slug."""
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    skill = {"name": "Big Task", "description": "d", "prompt_template": "do it", "slash": ""}
    monkeypatch.setattr(server.STATE, "skills_index", _SkillsIdx([skill]), raising=False)
    parsed = server._parse_skill_command("/big-task now")
    assert parsed is not None and parsed[0]["name"] == "Big Task" and parsed[1] == "now"


def test_workflow_of_same_token_wins(monkeypatch):
    class _Reg:
        def get(self, name):
            return {"name": name} if name == "research" else None

    monkeypatch.setattr(server.STATE, "workflow_registry", _Reg(), raising=False)
    monkeypatch.setattr(server.STATE, "skills_index", _SkillsIdx([_RESEARCH]), raising=False)
    assert server._parse_skill_command("/research X") is None


def test_subagent_of_same_token_wins(monkeypatch):
    from graph.subagents.config import SUBAGENT_REGISTRY

    collide = next(iter(SUBAGENT_REGISTRY))
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    shadow = {**_RESEARCH, "slash": collide}
    monkeypatch.setattr(server.STATE, "skills_index", _SkillsIdx([shadow]), raising=False)
    assert server._parse_skill_command(f"/{collide} X") is None


def test_goal_token_never_a_skill(monkeypatch):
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    shadow = {**_RESEARCH, "slash": "goal"}
    monkeypatch.setattr(server.STATE, "skills_index", _SkillsIdx([shadow]), raising=False)
    assert server._parse_skill_command("/goal do something") is None


def test_no_skills_index_returns_none(monkeypatch):
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(server.STATE, "skills_index", None, raising=False)
    assert server._parse_skill_command("/research X") is None


def test_skill_directive_injects_procedure_and_args():
    directive = server._skill_directive(_RESEARCH, "uv vs poetry")
    assert "web-research" in directive
    assert "Plan, search, read, cite." in directive
    assert "Input: uv vs poetry" in directive


def test_skill_directive_omits_input_when_no_args():
    directive = server._skill_directive(_RESEARCH, "")
    assert "Plan, search, read, cite." in directive
    assert "Input:" not in directive


# --- Unknown-command guard (#2893) --------------------------------------------
# Last in the dispatch chain: a `/foobar` that resolves to NO registered kind
# (goal / lifecycle / plugin command / workflow / subagent / skill) short-circuits
# the turn with a hint instead of silently becoming a plain agent turn.


def _clear_registries(monkeypatch, skills=None):
    monkeypatch.setattr(server.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(server.STATE, "plugin_chat_commands", {}, raising=False)
    monkeypatch.setattr(server.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(server.STATE, "skills_index", _SkillsIdx(skills or []), raising=False)


def test_unknown_slash_command_reply_fires_for_unregistered(monkeypatch):
    from server.chat import _unknown_slash_command_reply

    _clear_registries(monkeypatch, [_RESEARCH])
    assert (
        _unknown_slash_command_reply("/nonexistent")
        == "Unknown command /nonexistent. Type / to see available commands."
    )
    assert (
        _unknown_slash_command_reply("/foobar some args")
        == "Unknown command /foobar. Type / to see available commands."
    )


def test_unknown_slash_command_reply_spares_registered_kinds(monkeypatch):
    from graph.subagents.config import SUBAGENT_REGISTRY
    from server.chat import _unknown_slash_command_reply

    class _Reg:
        def get(self, name):
            return {"name": name} if name == "brief" else None

    async def _issue_handler(rest, session_id):  # noqa: ARG001 — signature parity
        return "issue reply"

    _clear_registries(monkeypatch, [_RESEARCH])
    monkeypatch.setattr(server.STATE, "plugin_chat_commands", {"issue": _issue_handler}, raising=False)
    monkeypatch.setattr(server.STATE, "workflow_registry", _Reg(), raising=False)
    assert _unknown_slash_command_reply("/goal status") is None  # reserved
    assert _unknown_slash_command_reply("/lifecycle") is None  # reserved
    assert _unknown_slash_command_reply("/issue 42") is None  # plugin command
    assert _unknown_slash_command_reply("/brief topic") is None  # workflow
    subagent = next(iter(SUBAGENT_REGISTRY))
    assert _unknown_slash_command_reply(f"/{subagent} do it") is None  # subagent
    assert _unknown_slash_command_reply("/research uv vs poetry") is None  # skill


def test_unknown_slash_command_reply_ignores_non_commands(monkeypatch):
    from server.chat import _unknown_slash_command_reply

    _clear_registries(monkeypatch)
    assert _unknown_slash_command_reply("just chatting") is None
    assert _unknown_slash_command_reply("check /home/user") is None
    assert _unknown_slash_command_reply("use uv/pip") is None
    assert _unknown_slash_command_reply("/home/user/file.txt") is None  # a path, not a command
    assert _unknown_slash_command_reply("/") is None
    assert _unknown_slash_command_reply("   ") is None


@pytest.mark.asyncio
async def test_unknown_command_short_circuits_streaming_turn(monkeypatch):
    """#2893: /nonexistent never reaches the graph — the stream is a single done
    frame carrying the unknown-command hint. STATE.graph is a bare sentinel, so
    any fall-through into the native turn would blow up the test."""
    import importlib

    chat_mod = importlib.import_module("server.chat")  # server.chat the ATTRIBUTE is the chat() function

    _clear_registries(monkeypatch, [_RESEARCH])
    monkeypatch.setattr(server.STATE, "graph", object(), raising=False)

    frames = [f async for f in chat_mod._chat_langgraph_stream("/nonexistent", "unknown-cmd-s1")]

    assert frames == [("done", "Unknown command /nonexistent. Type / to see available commands.")]


@pytest.mark.asyncio
async def test_unknown_command_short_circuits_collected_turn(monkeypatch):
    """#2893: same guard on the non-streaming path (console + OpenAI-compat)."""
    import importlib

    chat_mod = importlib.import_module("server.chat")

    _clear_registries(monkeypatch, [_RESEARCH])

    out = await chat_mod._chat_langgraph_impl("/foobar some args", "unknown-cmd-c1")

    assert out == [{"role": "assistant", "content": "Unknown command /foobar. Type / to see available commands."}]
