"""Goal lifecycle hooks (ADR 0028 D4, PR3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graph.goals.controller import GoalController
from graph.goals.hooks import fire_goal_hooks, set_goal_hooks
from graph.goals.store import GoalStore
from graph.goals.types import VerifyResult
from graph.goals.verifiers import set_plugin_verifiers
from graph.plugins.registry import PluginRegistry


@pytest.mark.asyncio
async def test_fire_routes_achieved_vs_failed():
    fired: list[str] = []
    set_goal_hooks(
        [{"plugin_id": "p", "on_achieved": lambda s: fired.append("ach"), "on_failed": lambda s: fired.append("fail")}]
    )
    try:
        await fire_goal_hooks("achieved", object())
        await fire_goal_hooks("exhausted", object())
        await fire_goal_hooks("unachievable", object())
        assert fired == ["ach", "fail", "fail"]
    finally:
        set_goal_hooks([])


@pytest.mark.asyncio
async def test_async_hook_runs_and_a_raising_hook_is_swallowed():
    seen: list[str] = []

    async def _ok(s):
        seen.append("ok")

    def _boom(s):
        raise RuntimeError("kaboom")

    set_goal_hooks(
        [
            {"plugin_id": "q", "on_achieved": _boom, "on_failed": None},  # raises first
            {"plugin_id": "p", "on_achieved": _ok, "on_failed": None},  # still runs
        ]
    )
    try:
        await fire_goal_hooks("achieved", object())  # must not raise
        assert seen == ["ok"]
    finally:
        set_goal_hooks([])


def test_registry_register_goal_hook_guards():
    reg = PluginRegistry("p", Path("."))
    reg.register_goal_hook(on_achieved=lambda s: None)
    reg.register_goal_hook()  # no callables → ignored
    reg.register_goal_hook(on_failed="nope")  # non-callable → ignored
    assert len(reg.goal_hooks) == 1


@pytest.mark.asyncio
async def test_controller_finish_fires_on_achieved(tmp_path):
    fired: list[str] = []
    set_goal_hooks([{"plugin_id": "p", "on_achieved": lambda s: fired.append(s.status), "on_failed": None}])

    async def _always_met(spec, ctx):
        return VerifyResult(True, "ok", "")

    set_plugin_verifiers({"p:always": _always_met})
    try:
        c = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
        c.set_goal_safe("s", "cond", {"type": "plugin", "check": "p:always"})
        decision = await c.evaluate("s", last_text="done")
        assert decision.action == "done" and decision.state.status == "achieved"
        assert fired == ["achieved"]
    finally:
        set_goal_hooks([])
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_finish_emits_goal_event_on_the_bus(tmp_path):
    """A terminal goal broadcasts goal.achieved/goal.failed (ADR 0039) so ANY plugin can react —
    no goal_hook required."""
    from graph.plugins.host import HOST

    events: list[tuple[str, dict]] = []

    async def _met(spec, ctx):
        return VerifyResult(True, "passed", "credits=1000000")

    set_plugin_verifiers({"p:always": _met})
    orig = HOST.publish
    HOST.publish = lambda topic, data: events.append((topic, data))
    try:
        c = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
        c.set_goal_safe("s", "reach target", {"type": "plugin", "check": "p:always"})
        await c.evaluate("s", last_text="done")
        # goal.achieved is broadcast on finish — alongside the lightweight goal.changed
        # store pushes (#1310), so find it rather than assume it's first.
        achieved = [d for t, d in events if t == "goal.achieved"]
        assert achieved, f"goal.achieved not on the bus: {[t for t, _ in events]}"
        assert achieved[0]["condition"] == "reach target"
        assert achieved[0]["status"] == "achieved"
    finally:
        HOST.publish = orig
        set_plugin_verifiers({})
