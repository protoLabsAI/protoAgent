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
    res = await run_verifier(
        {"type": "test", "command": "echo '5 passed in 1.2s'; exit 0"}, VerifyContext()
    )
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
