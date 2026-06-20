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
