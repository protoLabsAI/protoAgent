"""A background-spawned plugin subagent binds its declared tools, like ``task()`` (#4472).

brandLaunch (``tools.deferred.enabled``) spawned the campaign plugin's ``campaign_producer``
in the background and it came back "Campaign Studio tools are unavailable in this detached
context". A background job runs the LEAD graph under a ``subagent_fence`` naming the
subagent's allowlist; ``ToolDeferralMiddleware`` then trimmed that fenced request to its
base ``keep`` set, and ``search_tools`` (the only way to load a deferred tool) is outside
the fence. The foreground ``task()`` run builds its own graph with no deferral, so it had
every tool.

These drive the real path: the plugin tool rides ``extra_tools`` and the subagent sits in
``SUBAGENT_REGISTRY`` (where the plugin loader puts both), the real lead graph is built
with deferral on, ``BackgroundManager.spawn`` computes the fence, and that fence runs a
pass of the real graph. Only the self-POST (network) and the model are faked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from background.manager import BackgroundManager
from background.store import BackgroundStore
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, BackgroundSpawnRefused, SubagentConfig

_PLUGIN_TOOL = "fakeplugin_campaign_get"


@tool(_PLUGIN_TOOL)
def _plugin_tool(campaign_id: str = "") -> str:
    """Read a campaign plan (a plugin tool, deferred on the lead)."""
    return f"plan {campaign_id}"


class _RecordingFake(GenericFakeChatModel):
    """Records the tool names each model call was bound with."""

    bound: list = []

    def bind_tools(self, tools, **kwargs):
        type(self).bound.append(sorted(getattr(t, "name", None) or t.get("name") for t in tools))
        return self


@pytest.fixture
def lead(monkeypatch):
    import runtime.state as rs
    from langgraph.checkpoint.memory import MemorySaver

    cfg = LangGraphConfig(tools_deferred_enabled=True)
    _RecordingFake.bound = []
    fake = _RecordingFake(messages=iter([AIMessage(content="done")] * 4), disable_streaming=True)
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(cfg, extra_tools=[_plugin_tool], include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    return g


def _register(monkeypatch, name: str, tools: list[str]) -> None:
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        name,
        SubagentConfig(name=name, description="fake plugin producer", system_prompt="produce", tools=tools),
    )


def _manager(tmp_path: Path, monkeypatch) -> tuple[BackgroundManager, list]:
    fired: list = []

    async def _fire(self, job_id, prompt, fence, model=None):
        fired.append(fence)

    monkeypatch.setattr(BackgroundManager, "_fire", _fire)
    mgr = BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=BackgroundStore(str(tmp_path / "background" / "jobs.db")),
    )
    return mgr, fired


async def _drain(mgr: BackgroundManager) -> None:
    import asyncio

    for _ in range(50):
        if not mgr._fire_tasks:
            return
        await asyncio.sleep(0.01)


async def _model_tools_on_pass(g, fence: list[str] | None) -> list[str]:
    _RecordingFake.bound = []
    state = {"messages": [{"role": "user", "content": "produce asset 7"}], "session_id": "s"}
    if fence:
        state["subagent_fence"] = fence
    await g.ainvoke(state, {"configurable": {"thread_id": f"t-{bool(fence)}"}})
    assert _RecordingFake.bound, "the model was never called"
    return _RecordingFake.bound[0]


@pytest.mark.asyncio
async def test_background_spawn_binds_the_declared_plugin_tool(lead, monkeypatch, tmp_path):
    _register(monkeypatch, "fake_producer", ["current_time", "load_skill", _PLUGIN_TOOL])
    mgr, fired = _manager(tmp_path, monkeypatch)

    await mgr.spawn(origin_session="s1", subagent_type="fake_producer", description="d", prompt="p")
    await _drain(mgr)
    (fence,) = fired
    assert _PLUGIN_TOOL in fence

    # The detached turn: the real lead graph under the spawn's fence binds the plugin tool…
    bound = await _model_tools_on_pass(lead, fence)
    assert _PLUGIN_TOOL in bound
    assert "search_tools" not in bound
    # …while the lead's own unfenced pass is still deferred (the fix is fence-scoped).
    assert _PLUGIN_TOOL not in await _model_tools_on_pass(lead, None)


@pytest.mark.asyncio
async def test_spawn_refuses_when_no_declared_tool_is_bound(lead, monkeypatch, tmp_path):
    _register(monkeypatch, "fake_orphan", ["notinstalled_plugin_tool", "another_missing_tool"])
    mgr, fired = _manager(tmp_path, monkeypatch)

    with pytest.raises(BackgroundSpawnRefused) as exc:
        await mgr.spawn(origin_session="s1", subagent_type="fake_orphan", description="d", prompt="p")
    msg = str(exc.value)
    assert "fake_orphan" in msg and "notinstalled_plugin_tool" in msg and "none of them is bound" in msg
    assert fired == []
    assert mgr.store.list(include_dismissed=True) == []  # refused before anything is recorded


@pytest.mark.asyncio
async def test_partly_bound_allowlist_still_spawns_like_the_foreground(lead, monkeypatch, tmp_path):
    """A plugin may name another plugin's tools this agent lacks (campaign_producer names
    social_brand_kit); the foreground runs with what resolves, so the background does too."""
    _register(monkeypatch, "fake_partial", [_PLUGIN_TOOL, "social_brand_kit_not_installed"])
    mgr, fired = _manager(tmp_path, monkeypatch)

    jid = await mgr.spawn(origin_session="s1", subagent_type="fake_partial", description="d", prompt="p")
    await _drain(mgr)
    assert jid.startswith("bg-") and len(fired) == 1


@pytest.mark.asyncio
async def test_sdk_spawn_background_reports_the_refusal(lead, monkeypatch, tmp_path):
    import runtime.state as rs
    from graph import sdk

    _register(monkeypatch, "fake_orphan", ["notinstalled_plugin_tool"])
    mgr, _fired = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(rs.STATE, "background_mgr", mgr, raising=False)

    out = await sdk.spawn_background("p", subagent_type="fake_orphan", origin_session="s1")
    assert out["ok"] is False and out["task_id"] is None
    assert "notinstalled_plugin_tool" in out["message"]


@tool("current_time")
def _base_tool() -> str:
    """A base (always-kept) tool."""
    return "now"


@pytest.mark.parametrize(
    "fence, expected",
    [
        (None, ["current_time"]),  # the lead: deferred
        (["<no tools>"], ["current_time"]),  # deny-all: fence_tools trims nothing, so defer
        (["not_a_bound_tool"], ["current_time"]),  # fence names nothing bound: defer
        ([_PLUGIN_TOOL], ["current_time", _PLUGIN_TOOL]),  # a real fence sizes it: no deferral
    ],
)
def test_deferral_steps_aside_only_where_the_fence_trims(fence, expected):
    from langchain.agents.middleware.types import ModelRequest

    from graph.middleware.tool_deferral import ToolDeferralMiddleware

    state = {"messages": []}
    if fence:
        state["subagent_fence"] = fence
    req = ModelRequest(
        model=GenericFakeChatModel(messages=iter([])), messages=[], tools=[_base_tool, _plugin_tool], state=state
    )
    seen = []
    ToolDeferralMiddleware({"current_time"}).wrap_model_call(req, seen.append)
    assert sorted(t.name for t in seen[0].tools) == expected
