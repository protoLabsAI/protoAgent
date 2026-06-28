"""Verifier registry — goal mode."""

import json

import pytest

from graph.goals.verifiers import VerifyContext, run_verifier


@pytest.mark.asyncio
async def test_command_exit_zero_is_met():
    res = await run_verifier({"type": "command", "command": "exit 0"}, VerifyContext())
    assert res.met is True


@pytest.mark.asyncio
async def test_command_nonzero_not_met():
    res = await run_verifier({"type": "command", "command": "exit 3"}, VerifyContext())
    assert res.met is False
    assert "exited 3" in res.reason


@pytest.mark.asyncio
async def test_command_missing_field():
    res = await run_verifier({"type": "command"}, VerifyContext())
    assert res.met is False
    assert "missing" in res.reason


@pytest.mark.asyncio
async def test_test_verifier_surfaces_last_line():
    res = await run_verifier({"type": "test", "command": "echo '5 passed in 1.2s'; exit 0"}, VerifyContext())
    assert res.met is True
    assert "5 passed" in res.reason


@pytest.mark.asyncio
async def test_data_contains(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("status: DONE\n")
    met = await run_verifier({"type": "data", "path": str(f), "contains": "DONE"}, VerifyContext())
    missing = await run_verifier({"type": "data", "path": str(f), "contains": "NOPE"}, VerifyContext())
    assert met.met is True and missing.met is False


@pytest.mark.asyncio
async def test_data_expr(tmp_path):
    f = tmp_path / "out.json"
    f.write_text(json.dumps({"open": 0, "items": [1, 2, 3]}))
    res = await run_verifier(
        {"type": "data", "path": str(f), "expr": "data['open'] == 0 and len(data['items']) == 3"},
        VerifyContext(),
    )
    assert res.met is True


@pytest.mark.asyncio
async def test_data_expr_no_builtins_blocked(tmp_path):
    f = tmp_path / "out.json"
    f.write_text("{}")
    res = await run_verifier(
        {"type": "data", "path": str(f), "expr": "__import__('os').system('echo hi')"},
        VerifyContext(),
    )
    assert res.met is False
    assert "error" in res.reason.lower()


@pytest.mark.asyncio
async def test_data_expr_attribute_escape_blocked(tmp_path):
    """The classic eval-sandbox escape via attribute traversal is statically
    rejected (Attribute nodes disallowed) — not-met, never code execution."""
    f = tmp_path / "out.json"
    f.write_text("[]")
    for expr in (
        "().__class__.__bases__[0].__subclasses__()",
        "data.__class__",
        "'{0.__class__}'.format(data)",
    ):
        res = await run_verifier({"type": "data", "path": str(f), "expr": expr}, VerifyContext())
        assert res.met is False, expr
        assert "not allowed" in res.reason or "error" in res.reason.lower()


@pytest.mark.asyncio
async def test_data_missing_file(tmp_path):
    res = await run_verifier({"type": "data", "path": str(tmp_path / "nope.json"), "contains": "x"}, VerifyContext())
    assert res.met is False


@pytest.mark.asyncio
async def test_unknown_type():
    res = await run_verifier({"type": "bogus"}, VerifyContext())
    assert res.met is False
    assert "unknown" in res.reason


@pytest.mark.asyncio
async def test_ci_pr_checks(monkeypatch):
    async def fake_run_gh(args, timeout=60):
        assert args[:2] == ["pr", "checks"]
        return (0, "all checks passed", "")

    monkeypatch.setattr("tools.gh_cli.run_gh", fake_run_gh)
    res = await run_verifier({"type": "ci", "pr": 42}, VerifyContext())
    assert res.met is True


@pytest.mark.asyncio
async def test_ci_branch_run_conclusion(monkeypatch):
    async def fake_run_gh(args, timeout=60):
        return (0, json.dumps([{"status": "completed", "conclusion": "success", "name": "CI"}]), "")

    monkeypatch.setattr("tools.gh_cli.run_gh", fake_run_gh)
    res = await run_verifier({"type": "ci", "branch": "main"}, VerifyContext())
    assert res.met is True

    async def fake_fail(args, timeout=60):
        return (0, json.dumps([{"status": "completed", "conclusion": "failure"}]), "")

    monkeypatch.setattr("tools.gh_cli.run_gh", fake_fail)
    res2 = await run_verifier({"type": "ci", "branch": "main"}, VerifyContext())
    assert res2.met is False


@pytest.mark.asyncio
async def test_llm_verifier_fail_safe_without_config():
    res = await run_verifier({"type": "llm"}, VerifyContext(config=None))
    assert res.met is False


@pytest.mark.asyncio
async def test_llm_verifier_parses_json(monkeypatch):
    class _Resp:
        content = 'sure: {"met": true, "reason": "done"}'

    class _LLM:
        async def ainvoke(self, msgs, config=None):
            return _Resp()

    monkeypatch.setattr("graph.llm.create_llm", lambda config, model_name=None: _LLM())
    res = await run_verifier(
        {"type": "llm"},
        VerifyContext(config=object(), condition="ship it", last_text="shipped"),
    )
    assert res.met is True
    assert res.reason == "done"


# ── plugin-contributed verifiers (ADR 0028, PR1) ─────────────────────────────

from graph.goals.types import VerifyResult  # noqa: E402
from graph.goals.verifiers import set_plugin_verifiers  # noqa: E402


async def _ok_verifier(spec, ctx):
    return VerifyResult(True, "met", str(spec.get("args", {}).get("min", "")))


@pytest.mark.asyncio
async def test_plugin_verifier_dispatches():
    set_plugin_verifiers({"demo:check": _ok_verifier})
    try:
        res = await run_verifier({"type": "plugin", "check": "demo:check", "args": {"min": 5}}, VerifyContext())
        assert res.met is True and res.reason == "met" and res.evidence == "5"
    finally:
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_unknown_plugin_verifier_is_not_met():
    set_plugin_verifiers({})
    res = await run_verifier({"type": "plugin", "check": "nope"}, VerifyContext())
    assert res.met is False and "unknown" in res.reason


@pytest.mark.asyncio
async def test_plugin_verifier_error_never_marks_met():
    async def _boom(spec, ctx):
        raise RuntimeError("kaboom")

    set_plugin_verifiers({"demo:boom": _boom})
    try:
        res = await run_verifier({"type": "plugin", "check": "demo:boom"}, VerifyContext())
        assert res.met is False and "error" in res.reason
    finally:
        set_plugin_verifiers({})


def test_registry_auto_namespaces_and_guards():
    from pathlib import Path
    from graph.plugins.registry import PluginRegistry

    reg = PluginRegistry("myplugin", Path("."))
    reg.register_goal_verifier("credits", _ok_verifier)  # → myplugin:credits
    reg.register_goal_verifier("other:explicit", _ok_verifier)  # kept as-is
    reg.register_goal_verifier("", _ok_verifier)  # invalid — ignored
    reg.register_goal_verifier("bad", None)  # invalid — ignored
    assert set(reg.goal_verifiers) == {"myplugin:credits", "other:explicit"}
