"""Operator console handlers (ADR 0023 phase 3) — the bodies behind
register_operator_routes, extracted from _main into operator_api/console_handlers.py.
These exercise the STATE-driven degradation paths directly (no app needed)."""

import pytest

from operator_api import console_handlers as ch


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    import runtime.state as rs

    for field in ("graph_config", "graph", "scheduler", "goal_controller",
                  "workflow_registry", "inbox_store", "storm_guard", "skills_index"):
        monkeypatch.setattr(rs.STATE, field, None, raising=False)
    yield


def test_inbox_authorized_open_when_no_token():
    assert ch._inbox_authorized(None) is True
    assert ch._inbox_authorized("anything") is True


def test_inbox_authorized_requires_match(monkeypatch):
    import runtime.state as rs

    class _Cfg:
        auth_token = "secret"
    monkeypatch.setattr(rs.STATE, "graph_config", _Cfg(), raising=False)
    assert ch._inbox_authorized("secret") is True
    assert ch._inbox_authorized("nope") is False
    assert ch._inbox_authorized(None) is False


async def test_scheduler_list_disabled():
    assert await ch._operator_scheduler_list() == {"jobs": [], "backend": "disabled"}


async def test_goals_list_disabled():
    assert await ch._operator_goals_list() == {"goals": [], "enabled": False}


async def test_inbox_add_requires_store():
    with pytest.raises(RuntimeError):
        await ch._operator_inbox_add({"text": "hi"})


def test_chat_commands_lists_workflows_and_subagents(monkeypatch):
    import runtime.state as rs

    class _Reg:
        def list(self):
            return [{"name": "deep-research", "description": "d", "inputs": [{"name": "topic", "required": True}]}]

        def get(self, name):
            return next((w for w in self.list() if w["name"] == name), None)
    monkeypatch.setattr(rs.STATE, "workflow_registry", _Reg(), raising=False)
    out = ch._operator_chat_commands()
    names = [c["name"] for c in out["commands"]]
    assert "deep-research" in names
    dr = next(c for c in out["commands"] if c["name"] == "deep-research")
    assert dr["usage"] == "/deep-research <topic>"


def test_chat_commands_lists_user_facing_skills(monkeypatch):
    """User-facing skills surface as /<slash> commands; non-user-facing skills
    and collisions with a workflow/subagent name are skipped (ADR 0052)."""
    import runtime.state as rs

    class _SkillsIdx:
        def user_facing_skills(self):
            return [
                {"name": "web-research", "description": "Research the web.", "slash": "research"},
                {"name": "Big Task", "description": "Do a big task.", "slash": ""},
            ]

    monkeypatch.setattr(rs.STATE, "skills_index", _SkillsIdx(), raising=False)
    out = ch._operator_chat_commands()
    by_name = {c["name"]: c for c in out["commands"]}
    assert by_name["research"]["usage"] == "/research [input]"
    assert by_name["research"]["description"] == "Research the web."
    assert "big-task" in by_name  # blank slash → slugified name


def test_chat_commands_skill_defers_to_subagent_name(monkeypatch):
    """A user-facing skill whose token collides with a subagent is dropped —
    the subagent owns the slash token in dispatch."""
    import runtime.state as rs
    from graph.subagents.config import SUBAGENT_REGISTRY

    collide = next(iter(SUBAGENT_REGISTRY))  # a real subagent name (e.g. researcher)

    class _SkillsIdx:
        def user_facing_skills(self):
            return [{"name": collide, "description": "shadow", "slash": collide}]

    monkeypatch.setattr(rs.STATE, "skills_index", _SkillsIdx(), raising=False)
    out = ch._operator_chat_commands()
    # The command exists from the subagent, not the skill (skill description dropped).
    cmd = next(c for c in out["commands"] if c["name"] == collide)
    assert cmd["description"] != "shadow"
