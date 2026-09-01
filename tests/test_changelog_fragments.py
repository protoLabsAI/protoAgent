"""News fragments — the conflict-free replacement for the shared [Unreleased] anchor (#2322).

Every PR used to write to the same three lines, so two in flight conflicted by
construction and a stack of N cost O(N) serial merges. A fragment is a new file, which
has nothing to 3-way merge.
"""

from __future__ import annotations

import importlib.util
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


def test_lint_rejects_a_ref_that_sits_outside_the_bold_lead(tmp_path):
    """#3291: `scaffold` keeps only the bold lead, so a trailing ref is silently dropped.

    The fragment parses, collates and rolls perfectly — and then the marketing /changelog
    bullet is the only one of eleven with nothing to look up. Nothing downstream can
    recover the number, because the filename is gone by then. So catch it at the PR.
    """
    p = _frag(
        tmp_path,
        "3291.added.md",
        "- **Every Settings section is a ⌘K deep-link.** Details… (#3291)\n",
    )

    (problem,) = cl.lint_fragments([p])

    assert "no '#N' reference" in problem
    assert "(#3291)" in problem  # the message names the fix, using the filename's number
    # …and it really is dropped, which is WHY the lint exists.
    assert "#3291" not in cl._titles(p.read_text())[0]


def test_lint_accepts_a_ref_spelled_alongside_other_parenthetical_text(tmp_path):
    """`(v0.2.0, #3286)` is a real, fine spelling — the rule is "names a number", not one form."""
    p = _frag(tmp_path, "3286.changed.md", "- **orgChart rebuilt as a plugin (v0.2.0, #3286).** Details…")

    assert cl.lint_fragments([p]) == []


def test_lint_still_rejects_a_fragment_with_no_top_level_bullet(tmp_path):
    p = _frag(tmp_path, "3291.added.md", "Every Settings section is a deep-link (#3291).")

    (problem,) = cl.lint_fragments([p])

    assert "no top-level" in problem  # the older check, unchanged — and reported ALONE
