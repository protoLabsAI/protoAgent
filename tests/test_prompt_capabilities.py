"""Stable capability prose must follow the graph surface, including partial groups."""
from types import SimpleNamespace

import pytest

from graph.prompts import CAPABILITY_GROUPS, build_system_prompt_parts
from runtime.context import build_stable_prefix


@pytest.fixture(autouse=True)
def isolated_persona(monkeypatch):
    monkeypatch.setattr("graph.prompts._read_file", lambda _: "# Fixture identity")
    from runtime.state import STATE
    monkeypatch.setattr(STATE, "delegate_registry", SimpleNamespace(names=lambda: ["reviewer"]))


@pytest.mark.parametrize("bound", [frozenset(), frozenset({"current_time"})])
def test_empty_surface_has_no_capability_sections(bound):
    parts = dict(build_system_prompt_parts(bound_tool_names=bound,
                 projects=[{"name": "p", "path": "/p", "write": True}]))
    assert set(parts) == {"SOUL", "Guidelines"}


@pytest.mark.parametrize("tool", sorted(set().union(*CAPABILITY_GROUPS.values())))
def test_partial_groups_never_advertise_absent_members(tool):
    parts = dict(build_system_prompt_parts(bound_tool_names=frozenset({tool})))
    text = "\n".join(parts.values())
    for name in set().union(*CAPABILITY_GROUPS.values()):
        needle = "`wait(" if name == "wait" else f"`{name}`"
        if name == tool:
            assert needle in text
        else:
            assert needle not in text


@pytest.mark.parametrize("bound", [{"task"}, {"task_batch"}, {"task", "task_batch"}])
def test_independent_delegation_switches(bound):
    roster = dict(build_system_prompt_parts(bound_tool_names=frozenset(bound)))["Subagents"]
    for name in ("task", "task_batch"):
        assert (f"`{name}`" in roster) == (name in bound)


def test_configured_delegates_require_reachable_delegate_tool():
    assert "Collaboration" not in dict(build_system_prompt_parts(bound_tool_names=frozenset({"task"})))
    assert "Collaboration" in dict(build_system_prompt_parts(bound_tool_names=frozenset({"delegate_to"})))


def test_partial_filesystem_preserves_roots_without_advertising_writes():
    projects = [{"name": "p", "path": "/p", "write": True}]
    kwargs = dict(projects=projects, bound_tool_names=frozenset({"read_file", "find_files"}))
    text = dict(build_system_prompt_parts(**kwargs))["Managed projects"]
    assert "`read_file`" in text and "`find_files`" in text and "`/p`" in text
    assert "`write_file`" not in text and "`delete_file`" not in text
    assert "`list_dir`" not in text
    assert build_stable_prefix(**kwargs) == "\n\n".join(t for _, t in build_system_prompt_parts(**kwargs))
    # Configuration changes rebuild once; rebuilding unchanged inputs is byte-stable.
    changed = dict(kwargs, bound_tool_names=frozenset({"read_file", "find_files", "write_file"}))
    assert build_stable_prefix(**changed) != build_stable_prefix(**kwargs)
    assert build_stable_prefix(**changed) == build_stable_prefix(**changed)


def test_external_unknown_surface_is_explicitly_qualified():
    assert "use only tools exposed by your runtime" in build_stable_prefix(bound_tool_names=None)
