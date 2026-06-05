"""Tests for subagent slash-command parsing (ADR 0020 — /<subagent> in chat).

The companion to ``test_workflow_slash``: a chat message ``/researcher …`` runs
the named subagent (the composer analogue of the ``task`` tool) instead of a
normal model turn, so "run a worker" is a gesture, not a surface.
"""

from __future__ import annotations

import server


def test_known_subagent_parses_to_type_and_prompt():
    # SUBAGENT_REGISTRY always carries the builtin `researcher`; _workflow_registry
    # is None in a bare import, so there's no name collision to defer to.
    assert server._workflow_registry is None
    assert server._parse_subagent_command("/researcher find the latest on X") == (
        "researcher",
        "find the latest on X",
    )


def test_bare_name_yields_empty_prompt():
    # The dispatch turns an empty prompt into a usage hint rather than running.
    assert server._parse_subagent_command("/researcher") == ("researcher", "")


def test_unknown_name_and_non_command_return_none():
    assert server._parse_subagent_command("/definitely-not-a-subagent hi") is None
    assert server._parse_subagent_command("just chatting") is None
    assert server._parse_subagent_command("   ") is None


def test_workflow_of_same_name_takes_precedence(monkeypatch):
    """A workflow claiming the name wins — the turn dispatch checks workflows
    first, so _parse_subagent_command must defer (return None) to avoid running
    the worker for a name the workflow owns."""

    class _Reg:
        def get(self, name):
            return {"name": name} if name == "researcher" else None

    monkeypatch.setattr(server, "_workflow_registry", _Reg())
    assert server._parse_subagent_command("/researcher hi") is None
    # A subagent name the workflow registry doesn't claim still parses.
    assert server._parse_subagent_command("/antagonist poke holes in Y") == (
        "antagonist",
        "poke holes in Y",
    )
