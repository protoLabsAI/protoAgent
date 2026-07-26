"""The changelog PLACEMENT gate — entries must land under [Unreleased].

Sibling to test_changelog_gate.py, which covers WHETHER the changelog was touched.
This covers WHERE: a branch cut before a release roll carries `## [Unreleased]` as
its diff context, so once the roll renames that heading the 3-way merge files the
entry inside the already-closed section. Silent, and wrong twice over — credited to
a release that shipped without it, and missing from the next release's notes.

Two real occurrences motivated this (both features, both caught only by hand):
`d689b509` under [0.103.0] six days after that tag, and `6ba6c4d0` under [0.115.0].
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "changelog_placement", ROOT / "scripts" / "changelog_placement.py"
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MOD)

count = _MOD.entries_by_dated_section


def _doc(*sections: str) -> str:
    return "# Changelog\n\n" + "\n".join(sections)


_UNREL = "## [Unreleased]\n\n### Added\n- **New thing.** body\n"
_V2 = "## [0.2.0] - 2026-01-02\n\n### Fixed\n- **Old thing.** body\n"
_V1 = "## [0.1.0] - 2026-01-01\n\n### Added\n- **First thing.** body\n"


def test_counts_only_dated_sections():
    """[Unreleased] is open — it's supposed to accumulate entries, so it isn't counted."""
    got = count(_doc(_UNREL, _V2, _V1))
    # Keyed by VERSION, not heading text — a date correction must not change identity.
    assert got == {"0.2.0": 1, "0.1.0": 1}


def test_a_new_entry_under_a_dated_heading_is_detected():
    before = _doc(_UNREL, _V2, _V1)
    after = _doc(_UNREL, "## [0.2.0] - 2026-01-02\n\n### Fixed\n- **Old thing.** body\n- **Misfiled.** body\n", _V1)
    o, n = count(before), count(after)
    grew = [h for h in o if h in n and n[h] > o[h]]
    assert grew == ["0.2.0"]


def test_adding_under_unreleased_is_clean():
    """The correct case — the overwhelmingly common one — must never trip."""
    before = _doc(_UNREL, _V2)
    after = _doc("## [Unreleased]\n\n### Added\n- **New thing.** body\n- **Another.** body\n", _V2)
    o, n = count(before), count(after)
    assert not [h for h in o if h in n and n[h] > o[h]]


def test_rewording_an_existing_entry_is_clean():
    """Counts, not line matching: this repo does correct historical entries, and a
    reworded headline must not read as a new one."""
    before = _doc(_UNREL, _V2)
    after = _doc(_UNREL, "## [0.2.0] - 2026-01-02\n\n### Fixed\n- **Old thing, clarified.** better body\n")
    o, n = count(before), count(after)
    assert not [h for h in o if h in n and n[h] > o[h]]


def test_deleting_an_entry_is_clean():
    before = _doc(_UNREL, "## [0.2.0] - 2026-01-02\n\n### Fixed\n- **A.** x\n- **B.** y\n")
    after = _doc(_UNREL, "## [0.2.0] - 2026-01-02\n\n### Fixed\n- **A.** x\n")
    o, n = count(before), count(after)
    assert not [h for h in o if h in n and n[h] > o[h]]


def test_a_release_roll_is_clean_even_without_the_branch_exemption():
    """The roll RENAMES [Unreleased] to a dated heading, so that section is new in
    `after` and absent from `before` — the present-in-both rule skips it on its own.
    The release/* head-ref exemption is belt-and-braces, not the only defence."""
    before = _doc(_UNREL, _V1)
    after = _doc("## [Unreleased]\n", "## [0.2.0] - 2026-01-02\n\n### Added\n- **New thing.** body\n", _V1)
    o, n = count(before), count(after)
    assert not [h for h in o if h in n and n[h] > o[h]]


@pytest.mark.parametrize("ref", ["release/v1.2.3", "prepare-release/v0.116.0"])
def test_release_branches_are_exempt(monkeypatch, ref):
    monkeypatch.setenv("PR_HEAD_REF", ref)
    monkeypatch.setattr(_MOD.sys, "argv", ["changelog_placement.py", "origin/main"])
    assert _MOD.main() == 0


def test_workflow_wires_the_placement_gate():
    """Source guard: the check has to actually run in CI, in the workflow whose
    labeled/unlabeled triggers make the sibling gate re-runnable."""
    wf = (ROOT / ".github" / "workflows" / "changelog.yml").read_text(encoding="utf-8")
    assert "scripts/changelog_placement.py" in wf
    assert "PR_HEAD_REF" in wf


# ── review follow-up: identity by version, and no silent pass when it can't check ──


def test_a_date_correction_does_not_reset_section_identity():
    """Sections are keyed by VERSION, not the whole heading. Keying on heading text
    meant that correcting a date made the section look brand-new to the base/head
    comparison — so an entry misfiled in the same commit would slip through."""
    before = _doc(_UNREL, "## [0.2.0] - 2026-01-02\n\n### Fixed\n- **A.** x\n")
    after = _doc(_UNREL, "## [0.2.0] - 2026-01-03\n\n### Fixed\n- **A.** x\n- **Misfiled.** y\n")
    o, n = count(before), count(after)
    grew = [h for h in o if h in n and n[h] > o[h]]
    assert grew == ["0.2.0"], "a date-only heading edit must not hide a new entry"


def test_an_unresolvable_base_fails_rather_than_skipping(monkeypatch, tmp_path, capsys):
    """A gate that can't check must SAY so, not wave the PR through. Folding an
    unresolvable ref into 'nothing to compare' is the same fail-open shape this
    guard exists to prevent."""
    monkeypatch.delenv("PR_HEAD_REF", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    import subprocess as sp

    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setattr(_MOD.sys, "argv", ["changelog_placement.py", "origin/nope"])
    assert _MOD.main() == 1
    assert "does not resolve" in capsys.readouterr().out
