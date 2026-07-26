"""Tests for scripts/changelog_gate.sh (the PR changelog-entry CI gate).

The gate is a pure git+jq shell script so the `changelog` job in checks.yml
needs no dependency install; these tests drive it against throwaway git repos
the same way CI drives it against the PR checkout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "changelog_gate.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="changelog gate is a bash+git script — nothing to exercise without them",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _pr_repo(tmp_path: Path) -> Path:
    """A repo with CHANGELOG.md on main and a `feature` branch checked out —
    the shape the gate sees in CI (base ref resolvable, PR head at HEAD)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate@test")
    _git(repo, "config", "user.name", "gate")
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "checkout", "-qb", "feature")
    return repo


def _run_gate(
    repo: Path,
    *,
    head_ref: str = "feature",
    actor: str = "human",
    event: dict | None = None,
    event_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_EVENT_PATH"}
    env["PR_HEAD_REF"] = head_ref
    env["PR_ACTOR"] = actor
    if event is not None:
        assert event_dir is not None
        event_path = event_dir / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        env["GITHUB_EVENT_PATH"] = str(event_path)
    return subprocess.run(
        ["bash", str(SCRIPT), "main"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_fails_without_changelog_change_and_says_how_to_fix(tmp_path: Path) -> None:
    repo = _pr_repo(tmp_path)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "code only")

    result = _run_gate(repo)
    assert result.returncode == 1
    # The one-line fix instruction the job surfaces as a ::error:: annotation. It points
    # at a FRAGMENT now (#2322) — the conflict-free path — not the shared anchor.
    assert "changelog.d/" in result.stdout
    assert "skip-changelog" in result.stdout


def test_passes_when_pr_touches_changelog(tmp_path: Path) -> None:
    repo = _pr_repo(tmp_path)
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- a thing\n", encoding="utf-8"
    )
    _git(repo, "commit", "-aqm", "with entry")

    assert _run_gate(repo).returncode == 0


def test_changelog_change_on_base_after_fork_does_not_count(tmp_path: Path) -> None:
    """merge-base diff: someone ELSE's entry landing on main must not green a
    PR that added nothing itself."""
    repo = _pr_repo(tmp_path)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "code only")
    _git(repo, "checkout", "-q", "main")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- someone else's entry\n", encoding="utf-8"
    )
    _git(repo, "commit", "-aqm", "other PR's entry")
    _git(repo, "checkout", "-q", "feature")

    assert _run_gate(repo).returncode == 1


def test_skip_changelog_label_skips_the_gate(tmp_path: Path) -> None:
    if shutil.which("jq") is None:
        pytest.skip("label check reads the event payload via jq")
    repo = _pr_repo(tmp_path)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "code only")

    event = {"pull_request": {"labels": [{"name": "skip-changelog"}]}}
    assert _run_gate(repo, event=event, event_dir=tmp_path).returncode == 0


def test_other_labels_do_not_skip_the_gate(tmp_path: Path) -> None:
    if shutil.which("jq") is None:
        pytest.skip("label check reads the event payload via jq")
    repo = _pr_repo(tmp_path)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "code only")

    event = {"pull_request": {"labels": [{"name": "bug"}]}}
    assert _run_gate(repo, event=event, event_dir=tmp_path).returncode == 1


def test_release_branch_skips_the_gate(tmp_path: Path) -> None:
    repo = _pr_repo(tmp_path)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "code only")

    assert _run_gate(repo, head_ref="release/v0.112.0").returncode == 0


def test_dependabot_skips_the_gate(tmp_path: Path) -> None:
    repo = _pr_repo(tmp_path)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "code only")

    assert _run_gate(repo, actor="dependabot[bot]").returncode == 0


def test_changelog_yml_wires_the_gate_as_a_fast_pr_only_job() -> None:
    """Source guard on the workflow: the job exists, is PR-only, calls the
    script, and installs nothing (the whole point is failing in seconds).

    Lives in its OWN workflow rather than checks.yml so that applying the
    `skip-changelog` label re-triggers it — see the labeled/unlabeled guard below."""
    wf_dir = Path(__file__).parent.parent / ".github" / "workflows"
    workflow = (wf_dir / "changelog.yml").read_text(encoding="utf-8")
    assert "\n  changelog:\n" in workflow
    job = workflow.split("\n  changelog:\n", 1)[1]
    # PR-only by TRIGGER now (the workflow has no push:), not by a per-job `if`.
    assert "push:" not in workflow
    assert "pull_request:" in workflow
    assert "scripts/changelog_gate.sh" in job
    # fetch-depth: 0 so base...HEAD can resolve a merge-base on the PR checkout.
    assert "fetch-depth: 0" in job
    for install in ("pip install", "npm ci", "npm install", "setup-python", "setup-node"):
        assert install not in job, f"changelog job must stay install-free, found {install!r}"
    # checks.yml must NOT also define it — two jobs with the same check name would
    # race and report twice.
    assert "\n  changelog:\n" not in (wf_dir / "checks.yml").read_text(encoding="utf-8")


def test_labeling_retriggers_the_gate() -> None:
    """The whole reason the gate has its own workflow.

    The label is read from the event SNAPSHOT and neither `gh pr create --label`
    (labels are applied in a second api call, after `opened` fires) nor
    `gh run rerun` (replays the original payload) can get it in there. Without
    `labeled` in the trigger types, applying `skip-changelog` does nothing until
    someone pushes an empty commit."""
    workflow = (
        Path(__file__).parent.parent / ".github" / "workflows" / "changelog.yml"
    ).read_text(encoding="utf-8")
    types = workflow.split("types:", 1)[1].split("\n", 1)[0]
    for action in ("opened", "synchronize", "reopened", "labeled", "unlabeled"):
        assert action in types, f"changelog.yml must trigger on {action!r}, got {types.strip()}"


def test_passes_when_pr_adds_a_changelog_fragment(tmp_path: Path) -> None:
    """The path #2322 introduces: a new file per PR, so two PRs in flight can't conflict."""
    repo = _pr_repo(tmp_path)
    (repo / "changelog.d").mkdir(exist_ok=True)
    (repo / "changelog.d" / "2286.fixed.md").write_text("- fleet stop lied\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add fragment")

    result = _run_gate(repo)

    assert result.returncode == 0
    assert "fragment" in result.stdout


def test_the_fragments_readme_alone_does_not_satisfy_the_gate(tmp_path: Path) -> None:
    """Touching the docs file must not count as writing an entry."""
    repo = _pr_repo(tmp_path)
    (repo / "changelog.d").mkdir(exist_ok=True)
    (repo / "changelog.d" / "README.md").write_text("# docs edit\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "readme only")

    result = _run_gate(repo)

    assert result.returncode == 1
