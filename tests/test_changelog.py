"""Tests for scripts/changelog.py (the release-prep changelog roll)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("changelog", Path(__file__).parent.parent / "scripts" / "changelog.py")
changelog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(changelog)


_BASE = """# Changelog

intro text

## [Unreleased]

### Added
- a new thing

## [0.3.0] - 2026-05-01
### Added
- older thing
"""


def test_roll_promotes_unreleased_to_dated_section() -> None:
    out = changelog.roll(_BASE, "0.4.0", "2026-06-01")
    # New dated section exists with the moved content.
    assert "## [0.4.0] - 2026-06-01" in out
    assert "- a new thing" in out
    # Prior version section is untouched and stays below.
    assert "## [0.3.0] - 2026-05-01" in out
    assert out.index("## [0.4.0]") < out.index("## [0.3.0]")


def test_roll_leaves_fresh_empty_unreleased_on_top() -> None:
    out = changelog.roll(_BASE, "0.4.0", "2026-06-01")
    # Unreleased heading still present, now empty, and above the new version.
    assert "## [Unreleased]" in out
    assert out.index("## [Unreleased]") < out.index("## [0.4.0]")
    # The moved entry no longer sits under Unreleased.
    unreleased = out.split("## [Unreleased]", 1)[1].split("## [0.4.0]", 1)[0]
    assert "- a new thing" not in unreleased


def test_roll_handles_empty_unreleased() -> None:
    text = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n- seed\n"
    out = changelog.roll(text, "0.2.0", "2026-06-01")
    assert "## [0.2.0] - 2026-06-01" in out
    assert out.index("## [Unreleased]") < out.index("## [0.2.0]")


def test_roll_without_unreleased_raises() -> None:
    with pytest.raises(ValueError, match="Unreleased"):
        changelog.roll("# Changelog\n\n## [0.1.0] - 2026-01-01\n- x\n", "0.2.0", "2026-06-01")


def test_roll_does_not_pile_blank_lines() -> None:
    out = changelog.roll(_BASE, "0.4.0", "2026-06-01")
    assert "\n\n\n" not in out


_SCAFFOLD_MD = (
    "# Changelog\n\n"
    "## [Unreleased]\n\n### Added\n- not released yet\n\n"
    "## [0.2.0] - 2026-02-02\n\n"
    "### Added\n- **Bold title** — long technical detail (ADR 0026) with `code` and a [link](https://x).\n"
    "  a continuation line that should be ignored for the title\n"
    "  - a nested bullet that is not a top-level change\n"
    "### Fixed\n- plain fix without a bold lead, second clause\n"
)


def test_titles_are_concise_and_jargon_free():
    _date, body = changelog._section(_SCAFFOLD_MD, "0.2.0")
    # Bold lead becomes the title; long tail / ADR ref / nested bullet dropped.
    # Non-bold bullets keep their first clause (up to a dash or sentence end).
    assert changelog._titles(body) == ["Bold title", "plain fix without a bold lead, second clause"]


def test_scaffold_prepends_when_absent_and_is_idempotent(tmp_path, monkeypatch):
    import json

    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(_SCAFFOLD_MD, encoding="utf-8")
    mj = tmp_path / "changelog.json"
    mj.write_text(
        json.dumps([{"version": "v0.1.0", "date": "2026-01-01", "changes": ["curated blurb"]}]), encoding="utf-8"
    )
    monkeypatch.setattr(changelog, "CHANGELOG", cl)
    monkeypatch.setattr(changelog, "MARKETING_JSON", mj)

    assert changelog.scaffold("0.2.0") is True
    entries = json.loads(mj.read_text(encoding="utf-8"))
    assert [e["version"] for e in entries] == ["v0.2.0", "v0.1.0"]  # prepended
    assert entries[1]["changes"] == ["curated blurb"]  # existing curation untouched
    # Running again is a no-op (doesn't clobber a curated entry).
    assert changelog.scaffold("0.2.0") is False
    assert json.loads(mj.read_text(encoding="utf-8")) == entries


def test_titles_fold_a_bold_lead_that_wraps_lines():
    """A `**bold**` lead spanning two lines is captured whole (the v0.47/v0.53 glitch),
    and a same-line lead still works."""
    body = (
        "### Added\n"
        "- **A long bold lead that wraps\n"
        "  onto a second line.** then the rest of the bullet.\n"
        "- **Single line.** detail here\n"
        "  with a continuation that's ignored.\n"
    )
    assert changelog._titles(body) == ["A long bold lead that wraps onto a second line.", "Single line."]


def test_scaffold_omits_empty_release(tmp_path, monkeypatch):
    """A release whose section has no bullets is omitted from the marketing changelog
    (no bare version+date entry) rather than scaffolded empty."""
    import json

    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## [Unreleased]\n\n## [0.3.0] - 2026-03-03\n\n", encoding="utf-8")
    mj = tmp_path / "changelog.json"
    mj.write_text(json.dumps([{"version": "v0.1.0", "date": "2026-01-01", "changes": ["x"]}]), encoding="utf-8")
    monkeypatch.setattr(changelog, "CHANGELOG", cl)
    monkeypatch.setattr(changelog, "MARKETING_JSON", mj)

    assert changelog.scaffold("0.3.0") is False  # empty section → skipped
    assert [e["version"] for e in json.loads(mj.read_text(encoding="utf-8"))] == ["v0.1.0"]
    # …and an empty release absent from the json is NOT flagged as missing.
    assert changelog.missing_versions() == []


def test_notes_returns_section_body_markdown(tmp_path, monkeypatch):
    """`notes <version>` returns the curated CHANGELOG section (for the desktop updater)."""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(_SCAFFOLD_MD, encoding="utf-8")
    monkeypatch.setattr(changelog, "CHANGELOG", cl)

    body = changelog.notes("0.2.0")
    assert body.startswith("### Added")
    assert "**Bold title**" in body  # markdown preserved (UpdateNotice renders it)
    assert "## [0.2.0]" not in body  # the heading itself is not included
    assert "## [Unreleased]" not in body  # and it doesn't bleed into other sections


def test_notes_is_empty_for_missing_or_empty_section(tmp_path, monkeypatch):
    """Empty output signals the workflow to fall back (release body → placeholder)."""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## [Unreleased]\n\n## [0.3.0] - 2026-03-03\n\n", encoding="utf-8")
    monkeypatch.setattr(changelog, "CHANGELOG", cl)

    assert changelog.notes("0.3.0") == ""  # section exists but has no body
    assert changelog.notes("9.9.9") == ""  # section absent entirely


def test_no_released_version_is_missing_from_marketing_changelog():
    """Staleness guard (the original 'stuck at 0.21' bug): every dated CHANGELOG.md
    version must have a marketing changelog.json entry."""
    if not changelog.MARKETING_JSON.exists():
        pytest.skip("no marketing site (a fork dropped it) — staleness guard N/A")
    missing = changelog.missing_versions()
    assert not missing, f"changelog.json missing: {missing} — run `changelog.py scaffold <v>` then curate"


# ── Monthly archives (#2437) ─────────────────────────────────────────────────

_MULTI_MONTH = """# Changelog

intro text

## [Unreleased]

### Added
- pending thing

## [0.6.0] - 2026-08-01
### Added
- **August-first stays.** boundary is exclusive

## [0.5.0] - 2026-08-02
### Added
- **August feature.** detail

## [0.4.0] - 2026-07-31
### Fixed
- **July fix.** detail

## [0.3.0] - 2026-06-15
### Added
- **June thing.** oldest
"""


def _load_multi(tmp_path, monkeypatch):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(_MULTI_MONTH, encoding="utf-8")
    monkeypatch.setattr(changelog, "CHANGELOG", cl)
    return cl


def test_archive_splits_on_an_exclusive_date_boundary(tmp_path, monkeypatch):
    cl = _load_multi(tmp_path, monkeypatch)
    n, out = changelog.archive("2026-08-01", "CHANGELOG-THROUGH-2026-07.md")
    assert n == 2  # July + June move; both August sections (incl. the 2026-08-01 one) stay
    root, arch = cl.read_text(), out.read_text()
    assert changelog.dated_versions(root) == ["0.6.0", "0.5.0"]  # boundary is exclusive
    assert changelog.dated_versions(arch) == ["0.4.0", "0.3.0"]  # order preserved (newest first)
    # [Unreleased] + header stay in root; nothing rewritten
    assert "## [Unreleased]" in root and "- pending thing" in root
    assert "August feature." in root and "July fix." in arch and "June thing." in arch


def test_archive_partition_is_complete_and_unique(tmp_path, monkeypatch):
    """No release is dropped, duplicated, or reordered — the union equals the original."""
    cl = _load_multi(tmp_path, monkeypatch)
    original = changelog.dated_versions(cl.read_text())
    _n, out = changelog.archive("2026-08-01", "CHANGELOG-THROUGH-2026-07.md")
    union = changelog.dated_versions(cl.read_text()) + changelog.dated_versions(out.read_text())
    assert sorted(union) == sorted(original)
    assert len(union) == len(set(union))  # no duplicates


def test_archive_writes_cross_links(tmp_path, monkeypatch):
    cl = _load_multi(tmp_path, monkeypatch)
    _n, out = changelog.archive("2026-08-01", "CHANGELOG-THROUGH-2026-07.md")
    root, arch = cl.read_text(), out.read_text()
    assert changelog._ARCHIVE_INDEX_START in root  # root indexes the archive
    assert "[through 2026-07](CHANGELOG-THROUGH-2026-07.md)" in root
    assert "[CHANGELOG.md](CHANGELOG.md)" in arch  # archive links back


def test_archive_is_idempotent(tmp_path, monkeypatch):
    cl = _load_multi(tmp_path, monkeypatch)
    n1, out = changelog.archive("2026-08-01", "CHANGELOG-THROUGH-2026-07.md")
    root_after, arch_after = cl.read_text(), out.read_text()
    n2, _ = changelog.archive("2026-08-01", "CHANGELOG-THROUGH-2026-07.md")
    assert (n1, n2) == (2, 0)  # second run moves nothing
    assert cl.read_text() == root_after and out.read_text() == arch_after  # byte-identical


def test_notes_finds_an_archived_version(tmp_path, monkeypatch):
    """The desktop updater rebuild path: an OLD version's notes come from the archive
    after its section has rolled off the root file."""
    cl = _load_multi(tmp_path, monkeypatch)
    changelog.archive("2026-08-01", "CHANGELOG-THROUGH-2026-07.md")
    assert "0.4.0" not in changelog.dated_versions(cl.read_text())  # gone from root
    body = changelog.notes("0.4.0")  # still resolvable from the archive
    assert "July fix." in body
    assert changelog.notes("0.6.0")  # a current (root) version still resolves too
    assert changelog.notes("9.9.9") == ""  # absent everywhere → empty (fallback signal)


def test_missing_versions_is_a_current_only_contract(tmp_path, monkeypatch):
    """`missing_versions`/`check` guard against a forgotten NEW release, so they read only
    the current root file — an archived (already-curated) version is intentionally not
    re-flagged even though it's no longer in the root."""
    import json

    _load_multi(tmp_path, monkeypatch)  # monkeypatches CHANGELOG to the fixture
    mj = tmp_path / "changelog.json"
    # marketing has the two current versions but NOT the archived ones — must stay clean.
    mj.write_text(
        json.dumps(
            [
                {"version": "v0.6.0", "date": "2026-08-01", "changes": ["x"]},
                {"version": "v0.5.0", "date": "2026-08-02", "changes": ["y"]},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(changelog, "MARKETING_JSON", mj)
    changelog.archive("2026-08-01", "CHANGELOG-THROUGH-2026-07.md")
    assert changelog.missing_versions() == []  # archived versions are not re-demanded
