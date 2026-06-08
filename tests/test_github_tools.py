"""Tests for the GitHub read tools (gh CLI wrapper + tool contracts)."""

import json
from unittest.mock import patch

import pytest

from tools.github_tools import get_github_tools


def _tools():
    return {t.name: t for t in get_github_tools()}


@pytest.mark.asyncio
async def test_repo_required_no_silent_default():
    """Every tool rejects a missing/garbage repo without calling gh."""
    tools = _tools()
    with patch("tools.github_tools.run_gh") as run:
        for name, t in tools.items():
            arg = {"repo": "", "number": 1} if "number" in t.args else {"repo": ""}
            if name == "github_list_issues":
                arg = {"repo": ""}
            if name == "github_get_commit_diff":
                arg = {"repo": "", "ref": "abc"}
            if name == "github_run_failure":
                arg = {"repo": "", "run_id": 1}
            out = await t.ainvoke(arg)
            assert out.startswith("Error:"), name
            assert "no default" in out.lower() or "owner/name" in out.lower()
        run.assert_not_called()


@pytest.mark.asyncio
async def test_get_pr_happy_path():
    payload = json.dumps({
        "number": 7, "title": "Add thing", "state": "OPEN",
        "author": {"login": "octocat"}, "body": "does a thing",
        "additions": 10, "deletions": 2,
        "files": [{"path": "a.py"}, {"path": "b.py"}], "url": "http://x/7",
    })
    with patch("tools.github_tools.run_gh", return_value=(0, payload, "")):
        out = await _tools()["github_get_pr"].ainvoke({"repo": "o/r", "number": 7})
    assert "PR #7" in out and "Add thing" in out
    assert "octocat" in out and "+10/-2" in out
    assert "a.py" in out


@pytest.mark.asyncio
async def test_list_issues_empty_and_populated():
    t = _tools()["github_list_issues"]
    with patch("tools.github_tools.run_gh", return_value=(0, "[]", "")):
        assert "No open issues" in await t.ainvoke({"repo": "o/r"})
    items = json.dumps([
        {"number": 1, "title": "bug", "state": "OPEN", "labels": [{"name": "p0"}]},
        {"number": 2, "title": "feat", "state": "OPEN", "labels": []},
    ])
    with patch("tools.github_tools.run_gh", return_value=(0, items, "")):
        out = await t.ainvoke({"repo": "o/r", "state": "open"})
    assert "#1" in out and "p0" in out and "#2" in out


@pytest.mark.asyncio
async def test_list_issues_bad_state():
    out = await _tools()["github_list_issues"].ainvoke({"repo": "o/r", "state": "weird"})
    assert out.startswith("Error:") and "open|closed|all" in out


@pytest.mark.asyncio
async def test_gh_failure_surfaces_error_string():
    with patch("tools.github_tools.run_gh", return_value=(1, "", "not found")):
        out = await _tools()["github_get_issue"].ainvoke({"repo": "o/r", "number": 99})
    assert out.startswith("Error (gh exit 1)") and "not found" in out


@pytest.mark.asyncio
async def test_commit_diff_truncates():
    big = "diff --git a b\n" + ("+x\n" * 5000)
    with patch("tools.github_tools.run_gh", return_value=(0, big, "")):
        out = await _tools()["github_get_commit_diff"].ainvoke(
            {"repo": "o/r", "ref": "deadbeef", "max_chars": 100}
        )
    assert "truncated at 100" in out


@pytest.mark.asyncio
async def test_run_gh_missing_binary():
    """run_gh reports a clean error when gh isn't installed (no raise)."""
    from tools.gh_cli import run_gh
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        rc, out, err = await run_gh(["pr", "view", "1"])
    assert rc == 1 and "not installed" in err


@pytest.mark.asyncio
async def test_ci_runs_happy_path():
    payload = json.dumps([
        {"databaseId": 101, "name": "CI", "status": "completed", "conclusion": "failure",
         "headBranch": "main", "event": "push", "createdAt": "t", "url": "http://x/101"},
        {"databaseId": 102, "name": "CI", "status": "completed", "conclusion": "success",
         "headBranch": "main", "event": "push", "createdAt": "t", "url": "http://x/102"},
    ])
    with patch("tools.github_tools.run_gh", return_value=(0, payload, "")):
        out = await _tools()["github_ci_runs"].ainvoke({"repo": "o/r"})
    assert "#101 [failure]" in out and "http://x/101" in out
    assert "2 recent run(s)" in out


@pytest.mark.asyncio
async def test_ci_runs_empty():
    with patch("tools.github_tools.run_gh", return_value=(0, "[]", "")):
        out = await _tools()["github_ci_runs"].ainvoke({"repo": "o/r", "branch": "main"})
    assert "No recent runs" in out and "main" in out


@pytest.mark.asyncio
async def test_run_failure_extracts_error_lines():
    log = (
        "build\tCompile\t2026-01-01T00:00:00Z all good here\n"
        "test\tRun tests\t2026-01-01T00:00:01Z AssertionError: expected 1 to equal 2\n"
        "test\tRun tests\t2026-01-01T00:00:02Z 1 passed, 1 failed\n"
        "test\tRun tests\t2026-01-01T00:00:03Z just a normal line\n"
    )
    with patch("tools.github_tools.run_gh", return_value=(0, log, "")):
        out = await _tools()["github_run_failure"].ainvoke({"repo": "o/r", "run_id": 101})
    assert "AssertionError: expected 1 to equal 2" in out
    assert "1 passed, 1 failed" in out      # matched on "fail"
    assert "all good here" not in out        # non-error line filtered out


@pytest.mark.asyncio
async def test_run_failure_falls_back_to_tail():
    log = "job\tstep\t2026Z benign line one\njob\tstep\t2026Z benign line two\n"
    with patch("tools.github_tools.run_gh", return_value=(0, log, "")):
        out = await _tools()["github_run_failure"].ainvoke({"repo": "o/r", "run_id": 5})
    assert "benign line two" in out          # tail fallback when nothing matches
