"""filesystem.shell_first: the lead browses with the shell when run_command runs
unapproved. list_dir/find_files/search_files leave the LEAD's model view; subagents
(which never get run_command) and fenced passes keep them; read/edit/write stay."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph.config import LangGraphConfig
from graph.middleware.shell_first import SHELL_COVERED_TOOLS, ShellFirstMiddleware, shell_first_hidden

pytestmark = pytest.mark.platform_sensitive

LEAD = ("list_projects", "list_dir", "read_file", "find_files", "search_files", "edit_file", "write_file", "run_command")


def _cfg(**kw):
    base = {"filesystem_shell_first": True, "filesystem_run_requires_approval": False}
    return SimpleNamespace(**{**base, **kw})


def test_on_with_an_unapproved_shell_hides_exactly_the_three_browse_tools(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert shell_first_hidden(_cfg(), LEAD) == {"list_dir", "find_files", "search_files"}


@pytest.mark.parametrize(
    ("cfg", "names"),
    [
        (_cfg(filesystem_shell_first=False), LEAD),  # off by default
        (_cfg(filesystem_run_requires_approval=True), LEAD),  # every read would ask the operator
        (_cfg(), tuple(n for n in LEAD if n != "run_command")),  # no shell to fall back on
    ],
)
def test_inert_unless_a_free_shell_is_bound(cfg, names, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert shell_first_hidden(cfg, names) == frozenset()


def test_inert_on_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    assert shell_first_hidden(_cfg(), LEAD) == frozenset()


def test_the_config_defaults_off_and_parses_the_yaml_key():
    assert LangGraphConfig().filesystem_shell_first is False
    assert LangGraphConfig.from_dict({"filesystem": {"shell_first": True}}).filesystem_shell_first is True
    assert LangGraphConfig.from_dict({"filesystem": {"shell_first": "false"}}).filesystem_shell_first is False


class _Req(SimpleNamespace):
    def override(self, **kw):
        return _Req(**{**self.__dict__, **kw})


def _req(fence=None):
    return _Req(tools=[SimpleNamespace(name=n) for n in LEAD], state={"subagent_fence": fence} if fence else {})


MW = ShellFirstMiddleware(SHELL_COVERED_TOOLS)


def _names(r):
    return [t.name for t in r.tools]


def test_the_lead_model_view_drops_the_browse_tools_and_keeps_read_edit_write_and_the_shell():
    assert MW.wrap_model_call(_req(), _names) == ["list_projects", "read_file", "edit_file", "write_file", "run_command"]


def test_a_fenced_subagent_pass_keeps_them():
    assert MW.wrap_model_call(_req(fence=["search_files"]), _names) == list(LEAD)


async def test_async_matches():
    async def names(r):
        return _names(r)

    assert "search_files" not in await MW.awrap_model_call(_req(), names)


def test_subagents_still_inherit_the_hidden_reads():
    """Hiding is a model-view trim, not tools.subagent_only: inheritance is untouched."""
    from graph.tool_access import inherited_read_tools

    tools = [SimpleNamespace(name=n, metadata={}) for n in LEAD]
    inherited = {t.name for t in inherited_read_tools(tools, _cfg())}
    assert SHELL_COVERED_TOOLS <= inherited
    assert "run_command" not in inherited


@pytest.mark.parametrize("on", [True, False])
async def test_a_real_lead_turn_offers_the_model_the_shell_instead(tmp_path, monkeypatch, on):
    """End to end through create_agent_graph: with shell_first on, the schemas the lead
    model is bound with lack the three browse tools, while the ToolNode still holds them."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    from graph.agent import create_agent_graph

    monkeypatch.setattr("sys.platform", "darwin")
    offered: list[set[str]] = []

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            offered.append({getattr(t, "name", None) or t.get("name") for t in tools})
            return self

    cfg = LangGraphConfig(
        filesystem_enabled=True,
        filesystem_allow_run=True,
        filesystem_run_requires_approval=False,
        filesystem_shell_first=on,
        filesystem_projects=[{"name": "p", "path": str(tmp_path)}],
    )
    with patch("graph.agent.create_llm", lambda *a, **k: _Fake(messages=iter([AIMessage(content="ok")]))):
        g = create_agent_graph(cfg)
        await g.ainvoke({"messages": [HumanMessage(content="hi")]})
    assert offered
    assert {"read_file", "edit_file", "write_file", "run_command"} <= offered[-1]
    if on:
        assert not SHELL_COVERED_TOOLS & offered[-1]
    else:
        assert SHELL_COVERED_TOOLS <= offered[-1]
    assert SHELL_COVERED_TOOLS <= set(g.nodes["tools"].bound.tools_by_name)


def test_search_tools_never_lists_a_hidden_browse_tool_to_the_lead(tmp_path, monkeypatch):
    """Deferred mode: search_tools must not 'load' a tool ShellFirstMiddleware strips from
    every lead model call. A fenced background-subagent pass that names it still sees it."""
    from graph.agent import create_agent_graph
    from graph.fence_scope import fence_scope

    monkeypatch.setattr("sys.platform", "darwin")
    cfg = LangGraphConfig(
        filesystem_enabled=True,
        filesystem_allow_run=True,
        filesystem_run_requires_approval=False,
        filesystem_shell_first=True,
        filesystem_projects=[{"name": "p", "path": str(tmp_path)}],
        tools_deferred_enabled=True,
        tools_deferred_keep=[],
    )
    g = create_agent_graph(cfg)
    search = g.nodes["tools"].bound.tools_by_name["search_tools"]
    listed = str(search.invoke({"query": "", "limit": 50}))
    assert not any(f"`{n}`" in listed for n in SHELL_COVERED_TOOLS)
    with fence_scope(["search_files", "search_tools"]):
        assert "`search_files`" in str(search.invoke({"query": "search files", "limit": 50}))
