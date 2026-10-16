"""`draft_pr` on a managed-git acp delegate → `gh pr create --draft` (#4293).

A managed-git delegate could previously only be held to a draft-only/human-merge lane by
prompt instruction. The optional `draft_pr` flag makes it a real setting: when true the
harness appends `--draft` to `gh pr create`; when absent/false the create argv is
byte-identical to before.

Real-git style (mirrors tests/test_git_harness.py): a real `git init` repo with a bare
local "origin", built once per module and copied per test. Only the `gh` PR layer is faked
(monkeypatched `run_gh`)."""

from __future__ import annotations

import subprocess

import pytest

from plugins.coding_agent import git_harness as harness
from plugins.delegates.adapters import ADAPTERS, AcpAdapter
from tests._git_templates import copy_clone_with_origin

pytestmark = pytest.mark.platform_sensitive


def _git_sync(cwd, *args) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def repo_template(tmp_path_factory):
    """Built ONCE per module: a bare origin + a work clone with one pushed commit on main."""
    root = tmp_path_factory.mktemp("draft-pr-template")
    origin, work = root / "origin.git", root / "work"
    _git_sync(root, "init", "--bare", str(origin))
    _git_sync(root, "clone", str(origin), str(work))
    _git_sync(work, "config", "user.email", "test@example.com")
    _git_sync(work, "config", "user.name", "Test")
    (work / "README.md").write_text("hello\n")
    _git_sync(work, "add", "-A")
    _git_sync(work, "commit", "-m", "init")
    _git_sync(work, "branch", "-M", "main")
    _git_sync(work, "push", "-u", "origin", "main")
    return root


@pytest.fixture
def repo(repo_template, tmp_path):
    """A private work clone with its own bare origin, one pushed commit on main."""
    return copy_clone_with_origin(repo_template, tmp_path)


def _fake_gh(monkeypatch) -> list[list[str]]:
    """Fake ``run_gh`` recording every call; `pr list` empty, `pr create` succeeds."""
    calls: list[list[str]] = []
    responses = {"pr list": (0, "[]", ""), "pr create": (0, "https://github.com/x/y/pull/1", "")}

    async def fake(args, timeout=30, cwd=None):
        calls.append(list(args))
        return responses.get(" ".join(args[:2]), (1, "", "no fake response"))

    monkeypatch.setattr(harness, "run_gh", fake)
    return calls


def _pr_create(calls: list[list[str]]) -> list[str]:
    return next(a for a in calls if a[:2] == ["pr", "create"])


# ── parse: the delegate entry carries the flag ────────────────────────────────


def test_draft_pr_defaults_false_and_parses():
    d = ADAPTERS["acp"].parse({"name": "c", "type": "acp", "command": "proto", "workdir": "/tmp"})
    assert d.draft_pr is False  # absent ⇒ off
    d = ADAPTERS["acp"].parse(
        {"name": "c", "type": "acp", "command": "proto", "workdir": "/tmp", "draft_pr": "true"}
    )
    assert d.draft_pr is True


def test_draft_pr_is_an_advanced_schema_field():
    acp = {f.key: f for f in ADAPTERS["acp"].config_schema()}
    assert "draft_pr" in acp  # operator-facing: documented in Settings
    assert acp["draft_pr"].advanced is True


# ── harness: --draft is appended only when asked ──────────────────────────────


async def test_finish_appends_draft_flag(repo, monkeypatch):
    calls = _fake_gh(monkeypatch)
    await harness.prepare(str(repo), base="main", branch="t/draft-abc1234")
    (repo / "feature.txt").write_text("new\n")

    out = await harness.finish(
        str(repo), base="main", branch="t/draft-abc1234", item_id="abc1234", title="Add feature", draft=True
    )
    assert out.pushed and out.pr_state == "created"
    assert "--draft" in _pr_create(calls)


async def test_finish_without_draft_is_byte_identical(repo, monkeypatch):
    calls = _fake_gh(monkeypatch)
    await harness.prepare(str(repo), base="main", branch="t/plain-abc1235")
    (repo / "feature.txt").write_text("new\n")

    # Default (draft omitted) must match today's argv exactly — no --draft, fixed prefix.
    out = await harness.finish(
        str(repo), base="main", branch="t/plain-abc1235", item_id="abc1235", title="Add feature"
    )
    assert out.pushed
    create = _pr_create(calls)
    assert "--draft" not in create
    assert create[:6] == ["pr", "create", "--head", "t/plain-abc1235", "--base", "main"]
    assert create[6] == "--title" and create[8] == "--body"
    assert len(create) == 10  # pr create --head X --base main --title T --body B


# ── end-to-end: draft_pr on the delegate entry reaches gh ──────────────────────


async def test_managed_dispatch_draft_pr_true_reaches_gh(repo, monkeypatch):
    calls = _fake_gh(monkeypatch)

    async def fake_prompt(self, d, query, *, timeout=None):
        (repo / "built.txt").write_text("done\n")
        return "done"

    monkeypatch.setattr(AcpAdapter, "_prompt", fake_prompt)
    delegate = ADAPTERS["acp"].parse(
        {
            "name": "coder-draft",
            "type": "acp",
            "command": "fake",
            "workdir": str(repo),
            "manage_git": "true",
            "draft_pr": "true",
        }
    )
    try:
        reply = await AcpAdapter().dispatch(delegate, "Add the built file", item_id="draft-42")
    finally:
        harness.release("draft-42")
    assert "[managed git]" in reply
    assert "--draft" in _pr_create(calls)


async def test_managed_dispatch_draft_pr_absent_omits_draft(repo, monkeypatch):
    calls = _fake_gh(monkeypatch)

    async def fake_prompt(self, d, query, *, timeout=None):
        (repo / "built.txt").write_text("done\n")
        return "done"

    monkeypatch.setattr(AcpAdapter, "_prompt", fake_prompt)
    delegate = ADAPTERS["acp"].parse(
        {
            "name": "coder-plain",
            "type": "acp",
            "command": "fake",
            "workdir": str(repo),
            "manage_git": "true",
            # draft_pr omitted ⇒ default off
        }
    )
    try:
        reply = await AcpAdapter().dispatch(delegate, "Add the built file", item_id="plain-43")
    finally:
        harness.release("plain-43")
    assert "[managed git]" in reply
    assert "--draft" not in _pr_create(calls)
