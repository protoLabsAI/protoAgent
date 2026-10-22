"""Read inheritance candidates are default-deny and never bypass held tools."""

from types import SimpleNamespace

import pytest

from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from graph.tool_access import CORE_READ_TOOLS, NEVER_INHERITED, inherited_read_tools, is_read_tool
from tools.lg_tools import HITL_TOOL_NAMES


@pytest.mark.parametrize("name", sorted(CORE_READ_TOOLS))
def test_core_reads(name):
    assert is_read_tool(SimpleNamespace(name=name))


@pytest.mark.parametrize("metadata", [None, {}, {"access": "write"}, {"access": "READ"}])
@pytest.mark.parametrize("name", ["write_file", "edit_file", "delete_file", "update_artifact", "unknown"])
def test_unmarked_tools_are_denied(name, metadata):
    assert not is_read_tool(SimpleNamespace(name=name, metadata=metadata))


def test_metadata_seam_and_missing_metadata():
    assert is_read_tool(SimpleNamespace(name="plugin_read", metadata={"access": "read"}))
    assert not is_read_tool(SimpleNamespace(name="unknown"))
    # Core reads are authoritative even if metadata is absent or says otherwise.
    assert is_read_tool(SimpleNamespace(name="read_file", metadata={"access": "write"}))


@pytest.mark.parametrize(
    "name",
    sorted(HITL_TOOL_NAMES | {"task", "task_batch", "stop_task", "execute_code", "run_command", "delegate_to"}),
)
def test_control_tools_cannot_opt_into_inheritance(name):
    assert name in NEVER_INHERITED
    tool = SimpleNamespace(name=name, metadata={"access": "read"})
    assert inherited_read_tools([tool], SimpleNamespace(tools_subagent_only=[])) == []


@pytest.mark.parametrize("name", ["read_file", "plugin_read"])
def test_held_reads_require_explicit_allowlisting(name):
    tool = SimpleNamespace(name=name, metadata={"access": "read"})
    assert inherited_read_tools([tool], SimpleNamespace(tools_subagent_only=[name])) == []


def test_inheritance_preserves_order_and_first_eligible_object():
    plugin = SimpleNamespace(name="plugin_read", metadata={"access": "read"})
    core = SimpleNamespace(name="read_file")
    tools = [
        SimpleNamespace(name="plugin_read"),  # denied occurrence doesn't shadow a read
        plugin,
        SimpleNamespace(name="write_file"),
        core,
        SimpleNamespace(name="plugin_read", metadata={"access": "read"}),
        SimpleNamespace(name="read_file"),
    ]
    inherited = inherited_read_tools(tools, SimpleNamespace(tools_subagent_only=[]))
    assert len(inherited) == 2
    assert inherited[0] is plugin
    assert inherited[1] is core


@pytest.mark.parametrize("config", [SimpleNamespace(), SimpleNamespace(tools_subagent_only=None)])
def test_no_held_tools(config):
    tool = SimpleNamespace(name="read_file")
    assert inherited_read_tools([tool], config) == [tool]
    assert inherited_read_tools([], config) == []


def test_subagent_inherits_reads_by_default():
    # Preserve the existing required identity/prompt constructor arguments.
    assert SubagentConfig(name="test", description="test", system_prompt="test").inherit_read_tools is True


@pytest.mark.parametrize("name", ["dream", "distill", "self-improve", "review-synthesizer"])
def test_narrow_builtin_subagents_opt_out(name):
    assert SUBAGENT_REGISTRY[name].inherit_read_tools is False


def test_friction_triage_opts_out():
    from plugins.friction import _build_subagent

    assert _build_subagent().inherit_read_tools is False


def test_researcher_keeps_default_inheritance():
    assert SUBAGENT_REGISTRY["researcher"].inherit_read_tools is True
