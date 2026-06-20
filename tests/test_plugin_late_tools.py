"""The late-tools plugin seam (``register_late_tool_factory``).

A factory contributed via ``registry.register_late_tool_factory`` runs AFTER the
full toolset is assembled: it receives the resolved tool list + the live config,
and its result is appended (before the deferred ``search_tools`` meta-tool, so it
stays discoverable). This is the only hook that sees every other tool — for a
meta-tool that wraps/proxies the whole set (e.g. execute_code as a plugin).
"""

from pathlib import Path

from langchain_core.tools import tool

from graph.agent import create_agent_graph
from graph.config import LangGraphConfig
from graph.plugins.registry import PluginRegistry


@tool
def _sentinel(x: str) -> str:
    """A late-seam sentinel tool."""
    return x


def _bound_names(graph) -> list[str]:
    return [t.name for t in graph.bound_tools]


def test_late_factory_receives_full_toolset_and_its_tool_lands():
    seen: dict[str, list[str]] = {}

    def factory(all_tools, config):
        seen["names"] = [t.name for t in all_tools]
        return _sentinel

    graph = create_agent_graph(LangGraphConfig(), late_tool_factories=[factory])

    # The factory saw the fully-assembled core toolset...
    assert seen.get("names"), "factory should be called with the assembled toolset"
    assert "current_time" in seen["names"]  # a known core tool
    # ...and its tool was appended to the bound set.
    assert "_sentinel" in _bound_names(graph)


def test_late_factory_can_return_a_list_and_none_is_skipped():
    @tool
    def keeper(x: str) -> str:
        """t."""
        return x

    graph = create_agent_graph(
        LangGraphConfig(),
        late_tool_factories=[lambda tools, cfg: [keeper], lambda tools, cfg: None],
    )
    assert "keeper" in _bound_names(graph)


def test_raising_late_factory_is_isolated_and_later_ones_still_run():
    @tool
    def survivor(x: str) -> str:
        """t."""
        return x

    def boom(all_tools, config):
        raise RuntimeError("nope")

    graph = create_agent_graph(LangGraphConfig(), late_tool_factories=[boom, lambda t, c: survivor])
    assert "survivor" in _bound_names(graph)


def test_no_late_factories_is_a_no_op():
    # Default path (None) must not change the toolset or break the build.
    graph = create_agent_graph(LangGraphConfig())
    assert "current_time" in _bound_names(graph)
    assert "_sentinel" not in _bound_names(graph)


def test_registry_collects_late_factories_and_rejects_non_callables():
    reg = PluginRegistry("p", Path("."))
    reg.register_late_tool_factory(lambda tools, cfg: None)
    reg.register_late_tool_factory("not-callable")  # ignored with a warning
    assert len(reg.late_tool_factories) == 1
