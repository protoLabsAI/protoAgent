"""Tests for scripts/changelog_gate.sh (the PR changelog-entry CI gate).

The gate is a pure git+jq shell script so the `changelog` job in checks.yml
needs no dependency install; these tests drive it against throwaway git repos
the same way CI drives it against the PR checkout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.bashpath import real_bash

SCRIPT = Path(__file__).parent.parent / "scripts" / "changelog_gate.sh"

pytestmark = pytest.mark.skipif(
    real_bash() is None or shutil.which("git") is None,
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
        [real_bash(), str(SCRIPT), "main"],
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


def test_editing_changelog_directly_no_longer_satisfies_the_gate(tmp_path: Path) -> None:
    """#2322: an entry written under [Unreleased] is exactly the shared-anchor conflict
    fragments exist to remove, so it must not green a PR. The transitional acceptance was
    retired once the PR queue drained to zero — nothing was grandfathered."""
    repo = _pr_repo(tmp_path)
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n### Added\n- a thing\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "with entry")

    result = _run_gate(repo)

    assert result.returncode == 1
    assert "changelog.d/" in result.stdout


def test_a_changelog_edit_alongside_a_fragment_is_fine(tmp_path: Path) -> None:
    """Touching CHANGELOG.md isn't forbidden — fixing a typo in an old released section
    is legitimate. It just doesn't substitute for the fragment."""
    repo = _pr_repo(tmp_path)
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    (repo / "changelog.d").mkdir(exist_ok=True)
    (repo / "changelog.d" / "42.fixed.md").write_text("- a thing\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fragment + unrelated changelog touch")

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
    workflow = (Path(__file__).parent.parent / ".github" / "workflows" / "changelog.yml").read_text(encoding="utf-8")
    types = workflow.split("types:", 1)[1].split("\n", 1)[0]
    for action in ("opened", "synchronize", "reopened", "labeled", "unlabeled"):
        assert action in types, f"changelog.yml must trigger on {action!r}, got {types.strip()}"


# ── #2906: the gate must ALSO run inside a check that is actually required ───


def _checks_yml() -> str:
    return (Path(__file__).parent.parent / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")


def _workspace_config_job(checks: str) -> str:
    """The `workspace-config:` job body, up to the next top-level job key."""
    body = checks.split("\n  workspace-config:\n", 1)[1]
    return re.split(r"\n  [\w-]+:\n", body, maxsplit=1)[0]


def test_checks_yml_runs_the_gate_as_required_steps() -> None:
    """changelog.yml's `Changelog entry` check is NOT in main's
    required_status_checks, so auto-merge ignored a red gate — #2903 shipped a
    fragment the collator silently dropped. Both scripts therefore also run as
    steps of the workspace-config job, which IS required: a missing or malformed
    fragment now blocks merge, whatever the standalone workflow says."""
    job = _workspace_config_job(_checks_yml())
    assert "scripts/changelog_gate.sh" in job
    assert "scripts/changelog_placement.py" in job
    # merge-base diffing (base...HEAD) needs full history on the PR checkout.
    assert "fetch-depth: 0" in job
    # push-to-main has no PR diff to judge, and the guard must sit on the STEPS
    # (both of them) — an `if` skipping the whole job would leave a required
    # check unreported.
    assert job.count("if: github.event_name == 'pull_request'") >= 2


def test_checks_yml_gate_reads_labels_live_so_label_then_rerun_works() -> None:
    """checks.yml cannot take labeled/unlabeled trigger types (see its comments:
    every label flip would re-run the full suite), so here the skip-changelog
    escape hatch is 'apply the label, press Re-run failed jobs'. A rerun replays
    the ORIGINAL event payload, which can never contain a label applied after
    the run was created — the step must fetch labels live from the API and point
    the gate at them, or the hatch would demand an empty commit."""
    job = _workspace_config_job(_checks_yml())
    assert "pulls/${PR_NUMBER}" in job, "gate must fetch the PR's labels live, not trust the event snapshot"
    assert "GITHUB_EVENT_PATH=" in job, "the fetched labels must be handed to changelog_gate.sh"
    # The live fetch needs PR read on the job token; a job-level permissions
    # block REPLACES the workflow-level `contents: read`, so both must appear.
    assert "pull-requests: read" in job
    assert "contents: read" in job


def test_checks_yml_does_not_retrigger_on_label_changes() -> None:
    """The label-retrigger UX belongs to changelog.yml ALONE: labeled/unlabeled
    trigger types here would re-run the Python suite, fleet integration and web
    E2E on every label flip (and the split exists to prevent exactly that)."""
    triggers = _checks_yml().split("jobs:", 1)[0]
    assert "labeled" not in triggers


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


# ── #2600: existing isn't enough — the fragment has to survive collation ─────


def _add_fragment(repo: Path, name: str, body: str) -> None:
    (repo / "changelog.d").mkdir(exist_ok=True)
    (repo / "changelog.d" / name).write_text(body, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", f"add {name}")


def test_fragment_without_a_top_level_bullet_fails_the_gate(tmp_path: Path) -> None:
    """The real PR #2597 shape: a well-written fragment missing its leading '- '. It
    collates into [Unreleased] fine and then contributes NOTHING to the release notes,
    because scaffold derives entries from top-level bullets only. Green CI, silent loss."""
    repo = _pr_repo(tmp_path)
    _add_fragment(
        repo,
        "2555.added.md",
        "**Project onboarding config (#2555).** `onboarding` config section: `enabled`, `root`.\n",
    )

    result = _run_gate(repo)

    assert result.returncode == 1
    assert "top-level" in result.stdout or "top-level" in result.stderr
    assert "2555.added.md" in result.stdout + result.stderr  # names the offending file


def test_well_formed_fragment_still_passes(tmp_path: Path) -> None:
    repo = _pr_repo(tmp_path)
    _add_fragment(repo, "2600.fixed.md", "- **A real entry (#2600).** With a body that wraps\n  onto a second line.\n")

    result = _run_gate(repo)

    assert result.returncode == 0
    assert "ok: changelog.d/ fragment added" in result.stdout


def test_unknown_kind_fails_the_gate(tmp_path: Path) -> None:
    """`collate` already rejects this loudly at release time — catching it in the PR moves
    the error to the person who can fix it in one keystroke."""
    repo = _pr_repo(tmp_path)
    _add_fragment(repo, "2600.improved.md", "- **Something (#2600).** Body.\n")

    result = _run_gate(repo)

    assert result.returncode == 1
    assert "unknown kind" in result.stdout + result.stderr


def test_empty_fragment_fails_the_gate(tmp_path: Path) -> None:
    repo = _pr_repo(tmp_path)
    _add_fragment(repo, "2600.fixed.md", "\n\n")

    result = _run_gate(repo)

    assert result.returncode == 1


def test_deleting_someone_elses_fragment_does_not_satisfy_the_gate(tmp_path: Path) -> None:
    """A deletion isn't 'this PR documented itself' — and the path no longer exists to
    lint. Only the release branch legitimately removes fragments, and it has its own
    escape hatch."""
    repo = _pr_repo(tmp_path)
    _add_fragment(repo, "2599.fixed.md", "- **Prior entry (#2599).** Body.\n")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "feature")
    _git(repo, "checkout", "-qb", "feature2")
    (repo / "changelog.d" / "2599.fixed.md").unlink()
    _git(repo, "commit", "-aqm", "remove a fragment")

    result = _run_gate(repo, head_ref="feature2")

    assert result.returncode == 1


def test_prepare_release_branch_skips_the_gate(tmp_path: Path) -> None:
    """The branch prepare-release.yml actually creates is `prepare-release/vX.Y.Z`, which
    `release/*` does not match. It went unnoticed because a release PR DELETES every
    fragment and the old check counted any changed changelog.d path — so those PRs passed
    by accident, not by the escape hatch. #2600 stopped counting deletions and the accident
    stopped covering, breaking the v0.134.0 release PR."""
    repo = _pr_repo(tmp_path)
    _add_fragment(repo, "2600.fixed.md", "- **An entry (#2600).** Body.\n")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "feature")
    _git(repo, "checkout", "-qb", "prepare-release/v0.134.0")
    (repo / "changelog.d" / "2600.fixed.md").unlink()  # what `collate` does
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [0.134.0]\n- entry\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "chore: release v0.134.0")

    result = _run_gate(repo, head_ref="prepare-release/v0.134.0")

    assert result.returncode == 0
    assert "release branch" in result.stdout


def test_plain_release_branch_still_skips(tmp_path: Path) -> None:
    repo = _pr_repo(tmp_path)
    _git(repo, "checkout", "-qb", "release/v1.2.3")
    (repo / "code.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "release prep")

    assert _run_gate(repo, head_ref="release/v1.2.3").returncode == 0
