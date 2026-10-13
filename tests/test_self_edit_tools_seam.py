"""Seam guard for the ``tools/self_edit_tools.py`` extraction (#3839).

The curation / skill-editor / SOUL-editor / config-editor / fleet-diagnostics builders and
their helpers moved out of ``tools/lg_tools.py`` and are re-exported there, so
``lg_tools.<name>`` still RESOLVES — but a monkeypatch on ``lg_tools`` no longer INTERCEPTS a
collaborator the moved code calls by bare name: it resolves in the new module's globals. Such
a patch is dead, and the test around it can pass silently against the real thing. This scans
the suite so a stale target fails loudly.

Deliberately NOT stale: the ``_build_*`` builders themselves — ``get_all_tools`` stays in
``lg_tools`` and calls them by bare name, so a patch on ``lg_tools`` DOES intercept it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tools.lg_tools as lg_tools
import tools.self_edit_tools as self_edit_tools
from tests._seam_scan import stale_patches

# Collaborators whose only live callers moved: patch them on tools.self_edit_tools.
_MOVED_COLLABORATORS = {
    "_SOUL_MAX_BYTES",
    "_apply_soul_section_edit",
    "_publish_persona_event",
    "_CONFIG_WRITE_DENIED",
    "_CONFIG_WRITE_DENIED_LEAVES",
    "_config_write_refusal",
    "_session_id_from",
    "hashlib",
}

_RE_EXPORTED = (
    "_build_curation_tools",
    "_build_skill_editor_tools",
    "_build_soul_editor_tool",
    "_build_config_editor_tool",
    "_build_fleet_diagnostics_tool",
    "_SOUL_MAX_BYTES",
    "_apply_soul_section_edit",
    "_publish_persona_event",
    "_CONFIG_WRITE_DENIED",
    "_CONFIG_WRITE_DENIED_LEAVES",
    "_config_write_refusal",
)


def test_no_test_patches_a_moved_collaborator_on_lg_tools():
    stale = stale_patches("tools.lg_tools", _MOVED_COLLABORATORS)
    assert not stale, "patch these on tools.self_edit_tools, not lg_tools (#3839): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    """lg_tools re-exports the moved names by identity (no stale copies)."""
    for name in _RE_EXPORTED:
        assert getattr(lg_tools, name) is getattr(self_edit_tools, name), name


def test_operator_mcp_import_path_still_resolves():
    """``runtime/operator_mcp_tools.py`` imports the fleet-diagnostics builder from lg_tools."""
    from tools.lg_tools import _build_fleet_diagnostics_tool

    assert _build_fleet_diagnostics_tool is self_edit_tools._build_fleet_diagnostics_tool


def test_moved_module_does_not_import_lg_tools():
    """The new module sits BELOW lg_tools — importing it back would be a cycle and would
    capture lg_tools names by value at import time."""
    src = Path(self_edit_tools.__file__).read_text(encoding="utf-8")
    assert "lg_tools" not in "\n".join(
        line for line in src.splitlines() if line.lstrip().startswith(("import ", "from "))
    )


@pytest.mark.asyncio
async def test_moved_collaborator_resolves_in_its_new_home_at_call_time(monkeypatch):
    """``set_config`` looks ``_config_write_refusal`` up in self_edit_tools' globals at call
    time, so a patch THERE intercepts it (and one on lg_tools would not — the guard above)."""
    monkeypatch.setattr(self_edit_tools, "_config_write_refusal", lambda updates: "PATCHED REFUSAL")
    (set_config,) = self_edit_tools._build_config_editor_tool()
    assert await set_config.ainvoke({"updates": {"model.name": "x"}}) == "PATCHED REFUSAL"


def test_get_all_tools_still_calls_the_builders_by_bare_name(monkeypatch):
    """The assembly point stayed in lg_tools, so a patch on lg_tools intercepts it."""
    sentinel = object()
    monkeypatch.setattr(lg_tools, "_build_curation_tools", lambda **kw: [sentinel])
    monkeypatch.setattr(lg_tools, "_build_config_editor_tool", lambda: [sentinel])
    monkeypatch.setattr(lg_tools, "_build_skill_editor_tools", lambda **kw: [sentinel])
    monkeypatch.setattr(lg_tools, "_build_soul_editor_tool", lambda cb, **kw: [sentinel])
    tools = lg_tools.get_all_tools(self_config_enabled=True, skill_edit_enabled=True, soul_edit_enabled=True)
    assert tools.count(sentinel) == 4
