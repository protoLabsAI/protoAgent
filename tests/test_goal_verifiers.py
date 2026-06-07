"""Plugin-contributed goal verifiers (ADR 0028, PR1)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from graph.goals.types import VerifyResult
from graph.goals.verifiers import VerifyContext, run_verifier, set_plugin_verifiers
from graph.plugins.registry import PluginRegistry


async def _ok(spec, ctx):
    return VerifyResult(True, "met", str(spec.get("args", {}).get("min", "")))


def test_plugin_verifier_dispatches(monkeypatch):
    set_plugin_verifiers({"demo:check": _ok})
    res = asyncio.run(run_verifier({"type": "plugin", "check": "demo:check", "args": {"min": 5}}, VerifyContext()))
    assert res.met and res.reason == "met" and res.evidence == "5"
    set_plugin_verifiers({})  # cleanup


def test_unknown_plugin_verifier_is_not_met():
    set_plugin_verifiers({})
    res = asyncio.run(run_verifier({"type": "plugin", "check": "nope"}, VerifyContext()))
    assert not res.met and "unknown" in res.reason


def test_plugin_verifier_error_never_marks_met():
    async def _boom(spec, ctx):
        raise RuntimeError("kaboom")
    set_plugin_verifiers({"demo:boom": _boom})
    res = asyncio.run(run_verifier({"type": "plugin", "check": "demo:boom"}, VerifyContext()))
    assert not res.met and "error" in res.reason
    set_plugin_verifiers({})


def test_registry_auto_namespaces_and_guards():
    reg = PluginRegistry("myplugin", Path("."))
    reg.register_goal_verifier("credits", _ok)          # → myplugin:credits
    reg.register_goal_verifier("other:explicit", _ok)   # kept as-is
    reg.register_goal_verifier("", _ok)                  # invalid — ignored
    reg.register_goal_verifier("bad", None)             # invalid — ignored
    assert set(reg.goal_verifiers) == {"myplugin:credits", "other:explicit"}
