"""User-facing skill slash-token shadowing (bd-2zh).

A user-facing skill whose slash token collides with a workflow/subagent is
unreachable — the workflow/subagent wins dispatch and the skill is dropped from
the command palette. The shipped web-research skill hit this (`slash: research`
vs the deep-research workflow). These guard the rename + the warn-once net.
"""

from __future__ import annotations

from pathlib import Path

import operator_api.console_handlers as ch
import runtime.state as rs


def test_web_research_skill_slash_does_not_collide_with_research_workflow():
    text = Path("config/skills/web-research/SKILL.md").read_text()
    slash = next(
        (ln.split(":", 1)[1].strip() for ln in text.splitlines()
         if ln.strip().startswith("slash:")),
        None,
    )
    assert slash and slash != "research", f"web-research slash={slash!r} collides with /research"


def test_shadowed_user_facing_skill_is_skipped_and_warned(monkeypatch, caplog):
    class _WFReg:
        def list(self):
            return [{"name": "research", "description": "deep research", "inputs": []}]

        def get(self, name):
            return next((w for w in self.list() if w["name"] == name), None)

    class _SkillsIdx:
        def user_facing_skills(self):
            return [
                {"name": "web-research", "slash": "research", "description": "shadowed"},
                {"name": "gather", "slash": "gather", "description": "reachable"},
            ]

    import importlib

    sc = importlib.import_module("server.chat")  # the module (server.chat re-exports the chat fn)

    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "workflow_registry", _WFReg(), raising=False)
    monkeypatch.setattr(rs.STATE, "skills_index", _SkillsIdx(), raising=False)
    sc._warned_shadowed_skills.clear()

    with caplog.at_level("WARNING"):
        cmds = ch._operator_chat_commands()["commands"]

    names = [c["name"] for c in cmds]
    assert names.count("research") == 1          # only the workflow; skill skipped
    assert "gather" in names                      # non-colliding skill stays reachable
    assert any("unreachable" in r.getMessage() for r in caplog.records)
