"""``tools.disabled`` must cover the FULLY assembled toolset, not just ``get_all_tools``.

The denylist used to be applied only inside ``get_all_tools``, while the filesystem
tools (incl. the dual-use ``run_command``), plugin/MCP ``extra_tools``, delegation and
late-seam tools were appended AFTER it in ``graph.agent.create_agent_graph`` — so
``tools.disabled: [run_command]`` silently did nothing. These tests pin the fixed
contract: a disabled name is gone from the bound set no matter which seam contributed
it, and ``create_agent_graph`` derives the denylist from the config it's given.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from graph.agent import create_agent_graph
from graph.config import LangGraphConfig
from tools.lg_tools import drop_disabled_tools, get_all_tools, set_disabled_tools


@pytest.fixture(autouse=True)
def _reset_denylist():
    """The denylist is a module global (set from config at boot/reload/graph-build);
    reset it so a test's denylist never leaks into the rest of the suite."""
    yield
    set_disabled_tools([])


def _cfg(tmp_path, **over) -> LangGraphConfig:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return LangGraphConfig(
        filesystem_enabled=True,
        filesystem_allow_run=True,
        filesystem_projects=[{"name": "proj", "path": str(proj), "write": True}],
        **over,
    )


def _bound_names(graph) -> set[str]:
    return {t.name for t in graph.bound_tools}


# ── the fs seam (the original gap) ────────────────────────────────────────────


def test_run_command_bound_by_default(tmp_path):
    names = _bound_names(create_agent_graph(_cfg(tmp_path)))
    assert "run_command" in names


def test_tools_disabled_drops_run_command(tmp_path):
    names = _bound_names(create_agent_graph(_cfg(tmp_path, tools_disabled=["run_command"])))
    assert "run_command" not in names
    # Only the named tool is dropped — the rest of the fs toolset stays bound.
    assert "read_file" in names and "list_projects" in names


# ── the extra_tools (plugin/MCP) seam ─────────────────────────────────────────


def test_tools_disabled_drops_extra_tool(tmp_path):
    @tool
    def sample_plugin_tool() -> str:
        """A plugin-contributed tool."""
        return "ok"

    g = create_agent_graph(
        _cfg(tmp_path, tools_disabled=["sample_plugin_tool"]),
        extra_tools=[sample_plugin_tool],
    )
    assert "sample_plugin_tool" not in _bound_names(g)


# ── the delegation seam ───────────────────────────────────────────────────────


def test_tools_disabled_drops_task_tools(tmp_path):
    names = _bound_names(create_agent_graph(_cfg(tmp_path, tools_disabled=["task", "task_batch"])))
    assert "task" not in names and "task_batch" not in names


# ── the deferred meta-tool seam (appended after the final assembly pass) ─────


def test_tools_disabled_drops_search_tools(tmp_path):
    # search_tools is appended AFTER the final denylist pass (it's built over the
    # filtered set), so it needs its own pass — otherwise a disabled search_tools
    # silently re-binds, which the Tools-tab row toggle would surface as a switch
    # that snaps back on.
    g = create_agent_graph(
        _cfg(tmp_path, tools_deferred_enabled=True, tools_disabled=["search_tools"])
    )
    assert "search_tools" not in _bound_names(g)
    assert "search_tools" in {t.name for t in g.disabled_tools}


# ── the disabled catalog (Tools-tab rows stay toggleable) ─────────────────────


def test_disabled_tools_are_stamped_for_the_catalog(tmp_path):
    g = create_agent_graph(
        _cfg(tmp_path, tools_disabled=["run_command", "calculator", "ghost_tool"])
    )
    dropped = {t.name for t in g.disabled_tools}
    # The dropped tool OBJECTS are kept (name/description feed the console row) …
    assert {"run_command", "calculator"} <= dropped
    # … a denylisted name with no live tool has nothing to catalog …
    assert "ghost_tool" not in dropped
    # … and nothing cataloged as dropped leaks into the bound set.
    assert not (dropped & _bound_names(g))


def test_empty_denylist_stamps_an_empty_catalog(tmp_path):
    assert create_agent_graph(_cfg(tmp_path)).disabled_tools == []


# ── config-driven sync (no reliance on the server boot side effect) ──────────


def test_graph_build_syncs_denylist_from_config(tmp_path):
    # Simulate a stale process global from a PREVIOUS config: building with a config
    # whose denylist is empty must clear it, not inherit it.
    set_disabled_tools(["run_command"])
    names = _bound_names(create_agent_graph(_cfg(tmp_path)))
    assert "run_command" in names


# ── the primitives ────────────────────────────────────────────────────────────


def test_get_all_tools_still_filters_core():
    set_disabled_tools(["calculator"])
    assert "calculator" not in {t.name for t in get_all_tools(None)}


def test_drop_disabled_tools_noop_when_empty():
    set_disabled_tools([])
    tools = get_all_tools(None)
    assert drop_disabled_tools(tools) is tools  # same list — no copy on the hot path


def test_drop_disabled_tools_collects_dropped():
    @tool
    def alpha() -> str:
        """A."""
        return "a"

    @tool
    def beta() -> str:
        """B."""
        return "b"

    set_disabled_tools(["alpha"])
    dropped: list = []
    kept = drop_disabled_tools([alpha, beta], dropped)
    assert [t.name for t in kept] == ["beta"]
    assert [t.name for t in dropped] == ["alpha"]
