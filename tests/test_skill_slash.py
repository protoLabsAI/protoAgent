"""User-facing skill slash commands (ADR 0052 — /<skill> in chat).

Unlike /<workflow> and /<subagent> (which short-circuit the turn and run a
worker), a /<skill> command REWRITES the message to inject the skill's procedure
as a directive and falls through to the normal lead-agent turn. These tests
cover the parser + directive builder; the fall-through itself is exercised by
the streaming integration tests.
"""

from __future__ import annotations

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
