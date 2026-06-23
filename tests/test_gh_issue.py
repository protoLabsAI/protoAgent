"""Unit tests for the user-only ``/issue`` control command (tools/gh_issue.py).

Pure parse/validation logic + the create path with ``run_gh`` mocked — no
network, no real ``gh``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.gh_issue import (
    effective_default_repo,
    is_issue_command,
    missing_sections,
    parse_issue_control,
)


def test_effective_default_repo():
    # explicit default wins; else first non-blank in the list; else ""
    assert effective_default_repo("o/r", ["a/b", "c/d"]) == "o/r"
    assert effective_default_repo("", ["a/b", "c/d"]) == "a/b"
    assert effective_default_repo("", ["  ", "c/d"]) == "c/d"
    assert effective_default_repo("", []) == ""
    assert effective_default_repo("  ", None) == ""

_GOOD_BUG = (
    "/issue Scroll dead in delegate modal --bug --repo o/r\n"
    "## Problem\nThe wheel does nothing inside the modal.\n\n"
    "## Steps to reproduce\n1. open modal 2. scroll\n\n"
    "## Expected vs actual\nExpected scroll; nothing happens.\n\n"
    "## Acceptance\nWheel scrolls the modal body."
)


def test_is_issue_command():
    assert is_issue_command("/issue foo")
    assert is_issue_command("/issue")
    assert is_issue_command("/issue\nbody")
    assert not is_issue_command("/issues foo")  # not a prefix match
    assert not is_issue_command("hello /issue")
    assert not is_issue_command(None)  # type: ignore[arg-type]


def test_missing_sections_by_kind():
    body = "## Problem\nx" + " padding" * 20
    assert missing_sections(body, "generic") == []
    # bug needs repro/evidence/expected on top of Problem
    assert any("reproduce" in m.lower() for m in missing_sections(body, "bug"))
    # feature needs a proposed-direction or acceptance
    assert any("proposed" in m.lower() for m in missing_sections(body, "feature"))
    # a thin body is flagged regardless
    assert any("substantive" in m for m in missing_sections("## Problem\nx", "generic"))


async def test_non_issue_returns_none():
    assert await parse_issue_control("just chatting") is None
    assert await parse_issue_control("/goal win") is None


async def test_no_title_returns_usage_and_scaffold():
    out = await parse_issue_control("/issue", default_repo="o/r")
    assert "Usage:" in out and "## Problem" in out


async def test_no_repo_errors():
    out = await parse_issue_control("/issue Title\n## Problem\n" + "x" * 80)
    assert "No target repo" in out


async def test_bad_repo_errors():
    out = await parse_issue_control("/issue Title --repo not-a-repo\n## Problem\n" + "x" * 80)
    assert "owner/name" in out


async def test_missing_sections_block_creation():
    out = await parse_issue_control("/issue Title --bug --repo o/r\nthis body has no headings at all here")
    assert out.startswith("Not filed")
    assert "Steps to reproduce" in out  # scaffold for a bug


async def test_dry_run_does_not_call_gh():
    # --dry-run must be on the first line (the title+flags line), not the body.
    msg = _GOOD_BUG.replace("--bug --repo o/r", "--bug --repo o/r --dry-run", 1)
    with patch("tools.gh_issue.run_gh") as run:
        out = await parse_issue_control(msg)
    run.assert_not_called()
    assert out.startswith("Dry run")
    assert "o/r" in out and "bug" in out


async def test_happy_path_creates_and_labels():
    url = "https://github.com/o/r/issues/42"
    with patch("tools.gh_issue.run_gh", return_value=(0, url, "")) as run:
        out = await parse_issue_control(_GOOD_BUG)
    assert url in out and "Filed in o/r" in out
    args = run.call_args.args[0]
    assert args[:2] == ["issue", "create"]
    assert "--repo" in args and "o/r" in args
    assert "--label" in args and "bug" in args


async def test_default_repo_used_when_no_flag():
    with patch("tools.gh_issue.run_gh", return_value=(0, "https://x/1", "")) as run:
        out = await parse_issue_control(
            "/issue Title --feature\n## Problem\nwhy this matters enough to file it here and now\n\n"
            "## Proposed direction\nthe approach we would take to address it",
            default_repo="acme/widgets",
        )
    assert "acme/widgets" in out
    args = run.call_args.args[0]
    assert "acme/widgets" in args
    assert "enhancement" in args  # feature → enhancement label


async def test_gh_failure_surfaces_error():
    with patch("tools.gh_issue.run_gh", return_value=(1, "", "label 'bug' not found")):
        out = await parse_issue_control(_GOOD_BUG)
    assert out.startswith("Error") or "Hint" in out


@pytest.mark.parametrize("flag,label", [("--bug", "bug"), ("--feature", "enhancement"), ("--feat", "enhancement")])
async def test_type_flags_map_to_labels(flag, label):
    body = "## Problem\nwhy it matters here padding padding\n\n## Acceptance\ncriteria\n\n## Steps to reproduce\n1"
    with patch("tools.gh_issue.run_gh", return_value=(0, "https://x/1", "")) as run:
        await parse_issue_control(f"/issue Title {flag} --repo o/r\n{body}")
    assert label in run.call_args.args[0]
