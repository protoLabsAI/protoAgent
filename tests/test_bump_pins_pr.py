"""Tests for examples/bundles/template/scripts/bump_pins_pr.sh — the `bump` job's
PR-lifecycle logic (ADR 0049, #2645), extracted out of verify-bundle.yml's inline YAML
bash so a change to the dedup/approval-flagging contract has automated coverage instead
of a manual dry-run against a real GitHub repo (#2669).

Harness shape, following the repo's real-subprocess style (tests/test_changelog_gate.py):
a real throwaway git repo (bare "origin" + a work clone) so branch/commit/push behavior
is exercised for real, and a fake `gh` executable (tests/fixtures/fake_gh/gh) prepended
onto PATH — it logs every invocation to `calls.log` and returns scripted responses the
test writes into a per-test FAKE_GH_STATE_DIR beforehand. Only `gh` is faked; git is real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.bashpath import real_bash

SCRIPT = Path(__file__).parent.parent / "examples" / "bundles" / "template" / "scripts" / "bump_pins_pr.sh"
FAKE_GH_DIR = Path(__file__).parent / "fixtures" / "fake_gh"

pytestmark = pytest.mark.skipif(
    real_bash() is None or shutil.which("git") is None,
    reason="bump_pins_pr.sh is a bash+git+gh script — nothing to exercise without bash and git",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A work clone with a local bare origin, one pushed commit on main, and an
    UNSTAGED tracked-file change — the shape the real workflow sees: `check_bundle_updates.py`
    already rewrote the manifest in place, and `git commit -am` is about to pick it up."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "bot@test")
    _git(work, "config", "user.name", "bot")
    (work / "protoagent.bundle.yaml").write_text("members: []\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "seed")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "-u", "origin", "main")
    # `git init --bare` stamps the origin's HEAD at `init.defaultBranch` (git-version- and
    # config-dependent — "master" on some setups) and does NOT repoint it just because a
    # differently-named branch got pushed. A later `git clone` of that origin then follows
    # a HEAD pointing nowhere, and its checkout behavior on a dangling HEAD isn't
    # consistent across git versions — this bit us on CI's git while passing locally.
    # Pin it explicitly so every clone of `origin` unambiguously lands on `main`.
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    # The pin bump itself — what `check_bundle_updates.py` would have rewritten.
    (work / "protoagent.bundle.yaml").write_text("members:\n  - name: x\n    ref: v2\n", encoding="utf-8")
    return work


@pytest.fixture
def gh_state(tmp_path: Path) -> Path:
    d = tmp_path / "gh_state"
    d.mkdir()
    return d


def _run(
    repo: Path,
    gh_state: Path,
    *args: str,
    pr_list_queue: list[str] | None = None,
    run_list_response: list[dict] | None = None,
    pr_view_comment_count: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if pr_list_queue is not None:
        (gh_state / "pr_list_queue").write_text("\n".join(pr_list_queue) + ("\n" if pr_list_queue else ""))
    if run_list_response is not None:
        (gh_state / "run_list_response").write_text(json.dumps(run_list_response))
    if pr_view_comment_count is not None:
        (gh_state / "pr_view_comment_count").write_text(str(pr_view_comment_count))

    env = dict(os.environ)
    env["PATH"] = f"{FAKE_GH_DIR}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GH_STATE_DIR"] = str(gh_state)
    env["BUMP_PINS_SCRATCH_DIR"] = str(gh_state)  # keep pr_body.md/bumps.txt out of real /tmp
    env["BUMP_PINS_POLL_INTERVAL"] = "0"  # no real sleeping in tests
    env.pop("GITHUB_OUTPUT", None)
    env.update(extra_env or {})
    return subprocess.run(
        [real_bash(), str(SCRIPT), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def _calls(gh_state: Path) -> list[str]:
    log = gh_state / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


# ── open-or-update ──────────────────────────────────────────────────────────


def test_open_or_update_creates_a_pr_when_none_exists(repo: Path, gh_state: Path) -> None:
    # First `pr list` (before create) → nothing open; second (after create) → the new number.
    result = _run(repo, gh_state, "open-or-update", pr_list_queue=["", "42"])
    assert result.returncode == 0, result.stderr

    calls = _calls(gh_state)
    assert any(c.startswith("pr create") for c in calls)
    assert not any(c.startswith("pr edit") for c in calls)

    assert "number=42" in result.stdout
    assert "branch=bump-pins" in result.stdout
    assert "sha=" in result.stdout

    # The commit actually landed on a real `bump-pins` branch, force-pushed to origin.
    branches = subprocess.run(
        ["git", "branch", "-a"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert "bump-pins" in branches


def test_open_or_update_edits_the_existing_pr_instead_of_creating(repo: Path, gh_state: Path) -> None:
    result = _run(repo, gh_state, "open-or-update", pr_list_queue=["7"])
    assert result.returncode == 0, result.stderr

    calls = _calls(gh_state)
    assert any(c.startswith("pr edit 7 ") for c in calls)
    assert not any(c.startswith("pr create") for c in calls)
    assert "number=7" in result.stdout


def test_open_or_update_force_pushes_the_stable_branch(repo: Path, gh_state: Path) -> None:
    # Run it TWICE — the second run must reuse (force-push) the same remote branch, not
    # fail because it already exists (the whole point of #2645's dedup). Each run gets a
    # FRESH clone from the same origin, matching real CI: `actions/checkout@v4` re-clones
    # at the start of every job, so `bump-pins` never already exists in the LOCAL working
    # tree the way it would if this test reused one directory across both invocations.
    first = _run(repo, gh_state, "open-or-update", pr_list_queue=["", "1"])
    assert first.returncode == 0, first.stderr

    origin = repo.parent / "origin.git"
    second_clone = repo.parent / "work2"
    subprocess.run(["git", "clone", "-q", str(origin), str(second_clone)], check=True, capture_output=True)
    _git(second_clone, "config", "user.email", "bot@test")
    _git(second_clone, "config", "user.name", "bot")
    (second_clone / "protoagent.bundle.yaml").write_text("members:\n  - name: x\n    ref: v3\n", encoding="utf-8")

    result = _run(second_clone, gh_state, "open-or-update", pr_list_queue=["1"])
    assert result.returncode == 0, result.stderr


# ── flag-approval ────────────────────────────────────────────────────────────


def test_flag_approval_passes_quietly_when_the_run_actually_started(repo: Path, gh_state: Path) -> None:
    result = _run(
        repo,
        gh_state,
        "flag-approval",
        "9",
        "bump-pins",
        "deadbeef",
        run_list_response=[{"status": "completed", "conclusion": "success", "url": "https://x/run/1"}],
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(gh_state)
    assert any(c.startswith("run list") for c in calls)  # the poll itself always happens
    assert not any(c.startswith(("label create", "pr edit", "pr comment")) for c in calls)


def test_flag_approval_labels_and_comments_on_action_required(repo: Path, gh_state: Path) -> None:
    result = _run(
        repo,
        gh_state,
        "flag-approval",
        "9",
        "bump-pins",
        "deadbeef",
        run_list_response=[{"status": "completed", "conclusion": "action_required", "url": "https://x/run/2"}],
        pr_view_comment_count=0,
    )
    assert result.returncode == 1
    calls = _calls(gh_state)
    assert any(c.startswith("label create") for c in calls)
    assert any(c.startswith("pr edit 9 --add-label needs-approval") for c in calls)
    assert any(c.startswith("pr comment 9") for c in calls)
    assert "needs a maintainer to approve" in result.stdout


def test_flag_approval_does_not_double_comment(repo: Path, gh_state: Path) -> None:
    # pr_view_comment_count=1 → a prior run already left the comment; this run must still
    # label (idempotent, harmless) but NOT re-comment (the whole point of the dedup check).
    result = _run(
        repo,
        gh_state,
        "flag-approval",
        "9",
        "bump-pins",
        "deadbeef",
        run_list_response=[{"status": "completed", "conclusion": "action_required", "url": "https://x/run/3"}],
        pr_view_comment_count=1,
    )
    assert result.returncode == 1
    calls = _calls(gh_state)
    assert any(c.startswith("pr edit 9 --add-label needs-approval") for c in calls)
    assert not any(c.startswith("pr comment") for c in calls)


def test_flag_approval_fails_when_the_run_never_shows_up(repo: Path, gh_state: Path) -> None:
    result = _run(
        repo,
        gh_state,
        "flag-approval",
        "9",
        "bump-pins",
        "deadbeef",
        run_list_response=[],
        pr_view_comment_count=0,
    )
    assert result.returncode == 1
    assert "never showed up in the run list at all" in result.stdout
    calls = _calls(gh_state)
    assert any(c.startswith("pr comment") for c in calls)


def test_flag_approval_filters_run_list_on_the_exact_commit_sha(repo: Path, gh_state: Path) -> None:
    # The fake doesn't parse --commit itself (it just returns the scripted response
    # regardless of args), but this pins the CALL SHAPE: a regression that drops the
    # --commit filter (the #2645 fix for a stale-run false-positive) breaks this assertion.
    result = _run(
        repo,
        gh_state,
        "flag-approval",
        "9",
        "bump-pins",
        "cafef00d",
        run_list_response=[{"status": "completed", "conclusion": "success", "url": ""}],
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(gh_state)
    assert any("--commit cafef00d" in c for c in calls)
    assert any("--branch bump-pins" in c for c in calls)


def test_flag_approval_defaults_to_verify_bundle_workflow(repo: Path, gh_state: Path) -> None:
    result = _run(
        repo,
        gh_state,
        "flag-approval",
        "9",
        "bump-pins",
        "cafef00d",
        run_list_response=[{"status": "completed", "conclusion": "success", "url": ""}],
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(gh_state)
    assert any("--workflow verify-bundle.yml" in c for c in calls)


def test_flag_approval_respects_a_different_workflow_filename(repo: Path, gh_state: Path) -> None:
    # product-stack's pin-bump job lives in ci.yml, not verify-bundle.yml (#2669) — the
    # poll must search the workflow THIS repo actually runs, or it always reports the
    # "run never showed up" false-negative regardless of what actually happened.
    result = _run(
        repo,
        gh_state,
        "flag-approval",
        "9",
        "bump-pins",
        "cafef00d",
        run_list_response=[{"status": "completed", "conclusion": "success", "url": ""}],
        extra_env={"BUMP_PINS_WORKFLOW_FILE": "ci.yml"},
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(gh_state)
    assert any("--workflow ci.yml" in c for c in calls)
    assert not any("--workflow verify-bundle.yml" in c for c in calls)


def test_open_or_update_writes_outputs_to_github_output_file(repo: Path, gh_state: Path, tmp_path: Path) -> None:
    # emit() has two branches: stdout (what every other test above reads) and appending
    # to $GITHUB_OUTPUT — the branch the real workflow actually depends on
    # (steps.pr.outputs.number/branch/sha). _run() strips GITHUB_OUTPUT from the test
    # environment by default so tests don't clobber the real one if run under `act` or
    # similar; this test opts back in explicitly to cover that path.
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    result = _run(
        repo,
        gh_state,
        "open-or-update",
        pr_list_queue=["", "42"],
        extra_env={"GITHUB_OUTPUT": str(output_file)},
    )
    assert result.returncode == 0, result.stderr
    out = output_file.read_text()
    assert "number=42" in out
    assert "branch=bump-pins" in out
    assert "sha=" in out


def test_open_or_update_fails_fast_when_gh_pr_list_lags_behind_the_create(repo: Path, gh_state: Path) -> None:
    # gh pr list is eventually consistent: a `gh pr create` can return success before a
    # follow-up `gh pr list` sees the new PR. The script must fail loudly here rather than
    # emit `number=` (empty) — a downstream `flag-approval` call with an empty PR number
    # would silently no-op instead of flagging the real approval-required stall.
    result = _run(repo, gh_state, "open-or-update", pr_list_queue=["", ""])
    assert result.returncode == 1
    assert "could not read its number back" in result.stdout + result.stderr
    calls = _calls(gh_state)
    assert any(c.startswith("pr create") for c in calls)
