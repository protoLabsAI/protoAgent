"""``tools.subagent_only`` (ADR 0117): a named tool is never the LEAD's, yet every
subagent that allowlists it still gets it.

The point is containment: a domain's tools (and their schemas) live inside the subagent
that owns the domain, so the lead reaches them only through ``task``. These tests pin
each way the lead could otherwise still reach one: the bound set, the model request, a
direct call, the deferred ``search_tools`` meta-tool, a late tool that proxies others
(execute_code's bridge), and the operator MCP. They also pin both subagent paths: the
in-graph ``task`` snapshot, and a background run, which executes the LEAD graph under
its fence and so needs the held tools in its ToolNode.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

import graph.agent as agent_mod
from graph.agent import create_agent_graph, split_subagent_only
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from tools.lg_tools import set_disabled_tools


@tool
def media_grab(release: str) -> str:
    """Grab a release (a write the lead must not hold)."""
    return f"grabbed {release}"


@tool
def media_search(query: str) -> str:
    """Find a title."""
    return f"found {query}"


LIBRARIAN = SubagentConfig(
    name="librarian",
    description="Owns the media library.",
    system_prompt="p",
    tools=["media_grab", "media_search"],
    max_turns=5,
)


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setitem(SUBAGENT_REGISTRY, "librarian", LIBRARIAN)
    yield
    set_disabled_tools([])


def _graph(**over):
    cfg = LangGraphConfig(tools_subagent_only=["media_grab", "media_search"], **over)
    return create_agent_graph(cfg, extra_tools=[media_grab, media_search]), cfg


def _names(tools) -> set[str]:
    return {t.name for t in tools}


def test_the_lead_never_binds_a_subagent_only_tool():
    g, _ = _graph()
    bound = _names(g.bound_tools)
    assert not bound & {"media_grab", "media_search"}
    assert "task" in bound  # the lead's only way in
    assert _names(g.subagent_only_tools) == {"media_grab", "media_search"}


async def test_a_subagent_that_allowlists_one_still_gets_it(monkeypatch):
    g, _ = _graph()
    seen: dict = {}

    async def _capture(**kw):
        seen.update(kw)
        return "done"

    monkeypatch.setattr(agent_mod, "_run_subagent", _capture)
    task = next(t for t in g.bound_tools if t.name == "task")
    await task.coroutine(description="d", prompt="grab it", subagent_type="librarian", state=None, tool_call_id="c1")
    assert {"media_grab", "media_search"} <= set(seen["tool_map"])


def test_search_tools_never_surfaces_one_to_the_lead():
    g, _ = _graph(tools_deferred_enabled=True)
    search = next(t for t in g.bound_tools if t.name == "search_tools")
    out = search.invoke({"query": "media grab release"})
    assert "media_grab" not in str(out)


def test_a_late_proxying_tool_never_sees_one():
    snapshot: list[str] = []

    def factory(all_tools, _cfg):
        snapshot.extend(t.name for t in all_tools)  # what execute_code's bridge would proxy
        return None

    cfg = LangGraphConfig(tools_subagent_only=["media_grab"])
    create_agent_graph(cfg, extra_tools=[media_grab, media_search], late_tool_factories=[factory])
    assert "media_grab" not in snapshot and "media_search" in snapshot


def test_disabled_still_wins_over_subagent_only():
    g, _ = _graph(tools_disabled=["media_grab"])
    assert "media_grab" not in _names(g.bound_tools) | _names(g.subagent_only_tools)


def test_a_background_subagent_can_still_execute_one():
    """A detached subagent runs the lead graph under its fence (#1639), so the held tools
    must stay in the graph's ToolNode even though the lead never sees them."""
    g, _ = _graph()
    executable = set(g.nodes["tools"].bound.tools_by_name)
    assert {"media_grab", "media_search"} <= executable


def test_a_held_late_tool_or_search_tools_is_kept_off_the_lead(caplog):
    @tool
    def late_x() -> str:
        """A late-seam tool."""
        return ""

    SUBAGENT_REGISTRY["librarian"] = SubagentConfig(
        name="librarian", description="d", system_prompt="p", tools=["late_x", "search_tools"], max_turns=5
    )
    cfg = LangGraphConfig(tools_subagent_only=["late_x", "search_tools"], tools_deferred_enabled=True)
    g = create_agent_graph(cfg, late_tool_factories=[lambda _all, _cfg: late_x])
    assert not {"late_x", "search_tools"} & _names(g.bound_tools)
    assert {"late_x", "search_tools"} <= _names(g.subagent_only_tools)
    assert "only a background one" in caplog.text  # in-graph task can't resolve these


def test_load_skill_never_calls_a_held_tool_unavailable(monkeypatch):
    import runtime.state as rs
    from tools import lg_tools

    g, _ = _graph()
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    assert lg_tools._skill_tools_unavailable_note(["media_grab", "ghost_tool"]) == (
        "Unavailable in this context: ghost_tool (not bound in this context)."
    )


def test_unset_is_a_no_op():
    g = create_agent_graph(LangGraphConfig(), extra_tools=[media_grab])
    assert "media_grab" in _names(g.bound_tools)
    assert g.subagent_only_tools == []


def test_an_orphaned_name_is_logged(caplog):
    @tool
    def orphan() -> str:
        """No subagent lists me."""
        return ""

    lead, held = split_subagent_only([orphan, media_grab], LangGraphConfig(tools_subagent_only=["orphan", "media_grab"]))
    assert lead == [] and _names(held) == {"orphan", "media_grab"}
    assert "orphan" in caplog.text and "media_grab" not in caplog.text


def test_roundtrips_through_config(tmp_path):
    p = tmp_path / "langgraph-config.yaml"
    p.write_text("tools:\n  subagent_only: [media_grab, media_search]\n")
    assert LangGraphConfig.from_yaml(p).tools_subagent_only == ["media_grab", "media_search"]


def test_the_operator_mcp_never_serves_one(monkeypatch):
    from runtime import operator_mcp_tools as omt

    cfg = LangGraphConfig(tools_subagent_only=["media_grab"])
    import tools.lg_tools as lg

    monkeypatch.setattr(lg, "get_all_tools", lambda *a, **k: [])
    exposed = omt._exposed_tools(
        cfg,
        {"media_grab", "media_search"},
        knowledge_store=None,
        scheduler=None,
        inbox_store=None,
        tasks_store=None,
        plugin_tools=[media_grab, media_search],
    )
    assert _names(exposed) == {"media_search"}


async def test_a_real_lead_turn_never_offers_a_held_tool_to_the_model():
    """End to end: the schemas the model is bound with on a lead turn exclude the held
    tools (the middleware is installed), while the ToolNode still holds them."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    offered: list[set[str]] = []

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            offered.append({getattr(t, "name", None) or t.get("name") for t in tools})
            return self

    fake = _Fake(messages=iter([AIMessage(content="ok")]))
    cfg = LangGraphConfig(tools_subagent_only=["media_grab", "media_search"])
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        g = create_agent_graph(cfg, extra_tools=[media_grab, media_search])
        await g.ainvoke({"messages": [HumanMessage(content="hi")]})
    assert offered and all(not {"media_grab", "media_search"} & names for names in offered)
    assert "task" in offered[-1]


def test_search_tools_lists_a_held_tool_only_to_a_pass_fenced_to_it():
    """Deferred mode on a background subagent: deferral trims fenced passes too, so the
    subagent can only reach a held tool's schema by loading it through search_tools."""
    from graph.fence_scope import fence_scope

    g, _ = _graph(tools_deferred_enabled=True)
    search = next(t for t in g.nodes["tools"].bound.tools_by_name.values() if t.name == "search_tools")
    assert "media_grab" not in str(search.invoke({"query": "grab release"}))
    with fence_scope(["media_grab", "search_tools"]):
        assert "media_grab" in str(search.invoke({"query": "grab release"}))


async def _run_turn(fence=None):
    """One real turn on the lead graph whose model calls media_grab, then answers."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    fake = _Fake(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{"name": "media_grab", "args": {"release": "r5"}, "id": "c1"}]),
                AIMessage(content="done"),
            ]
        )
    )
    cfg = LangGraphConfig(tools_subagent_only=["media_grab", "media_search"])
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        g = create_agent_graph(cfg, extra_tools=[media_grab, media_search])
        state = {"messages": [HumanMessage(content="grab r5")]}
        if fence:
            state["subagent_fence"] = fence
        out = await g.ainvoke(state)
    return next(m for m in out["messages"] if isinstance(m, ToolMessage))


async def test_a_fenced_background_pass_runs_a_held_tool_end_to_end():
    result = await _run_turn(fence=["media_grab", "media_search"])
    assert result.content == "grabbed r5"


async def test_an_unfenced_lead_call_to_a_held_tool_is_blocked_end_to_end():
    result = await _run_turn()
    assert result.status == "error" and "subagent-only" in result.content and "grabbed" not in result.content
