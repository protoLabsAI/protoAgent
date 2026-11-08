"""News fragments — the conflict-free replacement for the shared [Unreleased] anchor (#2322).

Every PR used to write to the same three lines, so two in flight conflicted by
construction and a stack of N cost O(N) serial merges. A fragment is a new file, which
has nothing to 3-way merge.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "changelog_tool", Path(__file__).resolve().parents[1] / "scripts" / "changelog.py"
)
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)

BASE = """# Changelog

## [Unreleased]

## [0.1.0] - 2026-01-01

### Added
- something old
"""


def _frag(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


# ── reading fragments ─────────────────────────────────────────────────────────
def test_reads_and_groups_by_kind(tmp_path):
    _frag(tmp_path, "2286.fixed.md", "- fleet stop lied")
    _frag(tmp_path, "2119.added.md", "- /v1 session continuity")
    _frag(tmp_path, "2298.fixed.md", "- plugin drift")

    buckets, used = cl.read_fragments(tmp_path)

    assert sorted(buckets) == ["added", "fixed"]
    assert len(buckets["fixed"]) == 2 and len(used) == 3


def test_order_is_deterministic_by_filename(tmp_path):
    _frag(tmp_path, "b.fixed.md", "- bee")
    _frag(tmp_path, "a.fixed.md", "- ay")

    buckets, _ = cl.read_fragments(tmp_path)

    assert buckets["fixed"] == ["- ay", "- bee"]  # a release diff must not reshuffle


def test_readme_and_gitkeep_are_not_fragments(tmp_path):
    _frag(tmp_path, "README.md", "# docs")
    (tmp_path / ".gitkeep").write_text("")
    _frag(tmp_path, "1.fixed.md", "- real")

    buckets, used = cl.read_fragments(tmp_path)

    assert len(used) == 1 and buckets["fixed"] == ["- real"]


def test_an_unknown_kind_is_a_loud_error(tmp_path):
    """A fragment filed under a typo'd kind must not be silently dropped or mis-filed —
    a missing release-note entry is the exact failure the gate exists to prevent."""
    _frag(tmp_path, "9.fixxed.md", "- typo'd kind")

    with pytest.raises(ValueError, match="fixxed"):
        cl.read_fragments(tmp_path)


def test_a_malformed_filename_is_a_loud_error(tmp_path):
    _frag(tmp_path, "no-kind.md", "- nope")

    with pytest.raises(ValueError, match="expected"):
        cl.read_fragments(tmp_path)


def test_an_empty_fragment_is_ignored(tmp_path):
    _frag(tmp_path, "1.fixed.md", "\n  \n")
    buckets, used = cl.read_fragments(tmp_path)
    assert buckets == {} and used == []


def test_missing_directory_is_not_an_error(tmp_path):
    assert cl.read_fragments(tmp_path / "nope") == ({}, [])


# ── collation ─────────────────────────────────────────────────────────────────
def test_collate_creates_the_heading_when_absent():
    out = cl.collate(BASE, {"fixed": ["- fleet stop lied"]})

    assert "## [Unreleased]\n\n### Fixed\n- fleet stop lied" in out
    assert "## [0.1.0] - 2026-01-01" in out  # released sections untouched


def test_collate_merges_into_an_existing_heading_rather_than_duplicating_it():
    """Duplicate `### Fixed` blocks inside one Unreleased section were the other half of
    the shared-anchor problem — they had to be consolidated by hand every cycle."""
    text = BASE.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n- already here\n")

    out = cl.collate(text, {"fixed": ["- newly added"]})

    unreleased = out.split("## [0.1.0]")[0]
    assert unreleased.count("### Fixed") == 1
    assert "- already here" in unreleased and "- newly added" in unreleased


def test_collate_orders_kinds_consistently():
    out = cl.collate(BASE, {"docs": ["- d"], "added": ["- a"], "fixed": ["- f"]})
    unreleased = out.split("## [0.1.0]")[0]

    assert unreleased.index("### Added") < unreleased.index("### Fixed") < unreleased.index("### Docs")


def test_collate_with_no_fragments_is_a_no_op():
    assert cl.collate(BASE, {}) == BASE


def test_collate_then_roll_produces_a_dated_section():
    """The release path end to end: fragments land under Unreleased, then roll promotes."""
    collated = cl.collate(BASE, {"fixed": ["- fleet stop lied"]})

    rolled = cl.roll(collated, "0.2.0", "2026-07-26")

    assert "## [0.2.0] - 2026-07-26" in rolled
    assert "- fleet stop lied" in rolled.split("## [0.2.0]")[1].split("## [0.1.0]")[0]
    assert "## [Unreleased]" in rolled  # a fresh empty one remains


def test_collate_requires_an_unreleased_heading():
    with pytest.raises(ValueError, match="Unreleased"):
        cl.collate("# Changelog\n\n## [0.1.0] - 2026-01-01\n", {"fixed": ["- x"]})


# ── the point of the whole exercise ───────────────────────────────────────────
def test_two_prs_adding_fragments_do_not_touch_the_same_file(tmp_path):
    a = _frag(tmp_path, "2307.fixed.md", "- PR A")
    b = _frag(tmp_path, "2311.fixed.md", "- PR B")

    assert a != b  # distinct paths => nothing for git to 3-way merge
    buckets, used = cl.read_fragments(tmp_path)
    assert buckets["fixed"] == ["- PR A", "- PR B"] and len(used) == 2


# ── the lint gate: what a fragment must have to still say something at release ─
def test_lint_accepts_a_well_formed_fragment(tmp_path):
    p = _frag(tmp_path, "3291.added.md", "- **Every Settings section is a ⌘K deep-link (#3291).** Details…")

    assert cl.lint_fragments([p]) == []


def test_lint_accepts_a_bold_lead_with_no_ref_because_the_release_stamps_it(tmp_path):
    """#4874: the PR number doesn't exist at a PR's first commit, so the lead may omit it.

    The #3291 loss (a ref trailing the paragraph is dropped from /changelog, since only the
    bold lead survives) is now prevented at collate time, by stamping the PR number INTO the
    lead, instead of by making the author push a second commit to write it there.
    """
    p = _frag(
        tmp_path, "fix-settings-links.added.md", "- **Every Settings section is a ⌘K deep-link.** Details… (#3291)\n"
    )

    assert cl.lint_fragments([p]) == []
    assert "#4874" in cl._titles(cl.stamp_pr_ref(p.read_text(), 4874))[0]


def test_lint_rejects_a_bullet_with_neither_a_bold_lead_nor_a_ref(tmp_path):
    """Nothing to stamp into and nothing to cite: its /changelog line would name nothing."""
    p = _frag(tmp_path, "plain.fixed.md", "- **A good one.** body\n- a plain bullet - with no lead\n")

    (problem,) = cl.lint_fragments([p])

    assert "no **bold lead-in**" in problem and "plain.fixed.md" in problem


def test_lint_accepts_a_plain_bullet_that_cites_a_ref(tmp_path):
    p = _frag(tmp_path, "2286.fixed.md", "- fleet stop no longer lies (#2286). Details\n")

    assert cl.lint_fragments([p]) == []


@pytest.mark.parametrize("ref", ["#NNNN", "#TBD", "#XXX", "#PR", "#?"])
def test_lint_rejects_a_placeholder_pr_number(tmp_path, ref):
    """The stamp lands NEXT to a placeholder, so `(#NNNN) (#4880)` would ship."""
    p = _frag(tmp_path, "my-branch.fixed.md", f"- **A fix ({ref}).** Details\n")

    (problem,) = cl.lint_fragments([p])

    assert "placeholder" in problem


@pytest.mark.parametrize("name", ["fix-fleet-stop.fixed.md", "4848-adr.docs.md", "feat_x.y.added.md", "2286.fixed.md"])
def test_lint_accepts_slug_and_numbered_names(tmp_path, name):
    p = _frag(tmp_path, name, "- **A thing.** Details\n")

    assert cl.lint_fragments([p]) == []


@pytest.mark.parametrize("name", ["my fix.fixed.md", "-leading.fixed.md", "fix!.fixed.md", "fix.md"])
def test_lint_rejects_a_malformed_name(tmp_path, name):
    p = _frag(tmp_path, name, "- **A thing.** Details\n")

    (problem,) = cl.lint_fragments([p])

    assert "<slug>.<kind>.md" in problem


# ── #4874: the release stamps the PR number the fragment couldn't know ────────
def test_stamp_puts_the_ref_inside_the_lead_before_its_punctuation():
    body = (
        "- **Fix the thing.** body (#1)\n- **Has ref (#9).** x\n- **Wraps\n  over:** y\n- **No stop** z\n- plain (#3)"
    )

    out = cl.stamp_pr_ref(body, 42)

    assert out == (
        "- **Fix the thing (#42).** body (#1)\n- **Has ref (#9).** x\n"
        "- **Wraps\n  over (#42):** y\n- **No stop (#42)** z\n- plain (#3)"
    )
    assert cl._titles(out) == [
        "Fix the thing (#42).",
        "Has ref (#9).",
        "Wraps over (#42):",
        "No stop (#42)",
        "plain (#3)",
    ]


def test_stamp_leaves_nested_bullets_and_issue_refs_alone():
    body = "- **Cites its issue (#4874).** body\n  - **nested lead** stays\n"

    assert cl.stamp_pr_ref(body, 42) == body
    assert not cl.lead_needs_ref(body)


def test_read_fragments_stamps_only_leads_without_a_ref(tmp_path):
    _frag(tmp_path, "fix-a.fixed.md", "- **A.** body")
    _frag(tmp_path, "2286.fixed.md", "- **B (#2286).** body")
    asked: list[str] = []

    def resolve(path):
        asked.append(path.name)
        return 77

    buckets, _ = cl.read_fragments(tmp_path, resolve_pr=resolve)

    assert buckets["fixed"] == ["- **B (#2286).** body", "- **A (#77).** body"]
    assert asked == ["fix-a.fixed.md"]  # no git lookup for a fragment that needs none


def test_read_fragments_keeps_an_unresolvable_fragment_and_warns(tmp_path, capsys):
    _frag(tmp_path, "fix-a.fixed.md", "- **A.** body")

    buckets, used = cl.read_fragments(tmp_path, resolve_pr=lambda _p: None)

    assert buckets["fixed"] == ["- **A.** body"] and len(used) == 1
    assert "fix-a.fixed.md" in capsys.readouterr().err


def test_read_fragments_without_a_resolver_is_unchanged(tmp_path):
    _frag(tmp_path, "fix-a.fixed.md", "- **A.** body")

    assert cl.read_fragments(tmp_path)[0]["fixed"] == ["- **A.** body"]


# The resolver reads real git history, so these drive a throwaway repo.
def _needs_git(fn):
    return pytest.mark.platform_sensitive(pytest.mark.skipif(shutil.which("git") is None, reason="needs git")(fn))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "changelog.d").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "CHANGELOG.md").write_text(BASE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _commit_fragment(repo: Path, name: str, body: str, subject: str) -> Path:
    path = repo / "changelog.d" / name
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", subject)
    return path


@_needs_git
def test_pr_for_fragment_reads_the_squash_merge_subject(tmp_path):
    repo = _repo(tmp_path)
    path = _commit_fragment(repo, "fix-a.fixed.md", "- **A.** b\n", "fix: a thing (#4880)")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later work (#4890)")

    assert cl.pr_for_fragment(path) == 4880


@_needs_git
def test_pr_for_fragment_reads_a_merge_commit(tmp_path):
    """A fragment that came in on a merged branch belongs to the merge, not the branch commit."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-qb", "feature")
    path = _commit_fragment(repo, "feat-x.added.md", "- **X.** b\n", "feat: x on the branch (#12)")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #4881 from org/feat-x")

    assert cl.pr_for_fragment(path) == 4881


@_needs_git
def test_pr_for_fragment_uses_the_newest_add_of_a_reused_name(tmp_path):
    """`collate` deletes fragments, so a name can come back in a later PR."""
    repo = _repo(tmp_path)
    path = _commit_fragment(repo, "fix-a.fixed.md", "- **old.** b\n", "fix: old (#100)")
    path.unlink()
    _git(repo, "commit", "-qam", "chore: release v0.1.1 (#101)")
    _commit_fragment(repo, "fix-a.fixed.md", "- **new.** b\n", "fix: new (#200)")

    assert cl.pr_for_fragment(path) == 200


@_needs_git
def test_pr_for_fragment_is_none_for_a_direct_push_or_uncommitted_file(tmp_path):
    repo = _repo(tmp_path)
    pushed = _commit_fragment(repo, "fix-a.fixed.md", "- **A.** b\n", "fix: pushed straight to main")
    loose = repo / "changelog.d" / "loose.fixed.md"
    loose.write_text("- **L.** b\n", encoding="utf-8")

    assert cl.pr_for_fragment(pushed) is None
    assert cl.pr_for_fragment(loose) is None


@_needs_git
def test_collate_cli_stamps_from_git_history(tmp_path, monkeypatch):
    """End to end through `changelog.py collate`, the step prepare-release.yml runs."""
    repo = _repo(tmp_path)
    _commit_fragment(repo, "fix-a.fixed.md", "- **Fleet stop tells the truth.** Details.\n", "fix: fleet stop (#4880)")
    _commit_fragment(
        repo, "4874.changed.md", "- **Fragments are named by slug (#4874).** Details.\n", "chore: x (#4882)"
    )
    monkeypatch.setattr(cl, "CHANGELOG", repo / "CHANGELOG.md")
    monkeypatch.setattr(cl, "FRAGMENTS_DIR", repo / "changelog.d")
    monkeypatch.setattr("sys.argv", ["changelog.py", "collate"])

    cl.main()

    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- **Fleet stop tells the truth (#4880).** Details." in text
    assert "- **Fragments are named by slug (#4874).** Details." in text  # an explicit ref is kept
    assert not list((repo / "changelog.d").glob("*.md"))


def test_lint_accepts_a_ref_spelled_alongside_other_parenthetical_text(tmp_path):
    """`(v0.2.0, #3286)` is a real, fine spelling — the rule is "names a number", not one form."""
    p = _frag(tmp_path, "3286.changed.md", "- **orgChart rebuilt as a plugin (v0.2.0, #3286).** Details…")

    assert cl.lint_fragments([p]) == []


def test_lint_still_rejects_a_fragment_with_no_top_level_bullet(tmp_path):
    p = _frag(tmp_path, "3291.added.md", "Every Settings section is a deep-link (#3291).")

    (problem,) = cl.lint_fragments([p])

    assert "no top-level" in problem  # the older check, unchanged — and reported ALONE
