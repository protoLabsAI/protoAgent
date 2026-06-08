"""Tests for scripts/changelog.py (the release-prep changelog roll)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "changelog", Path(__file__).parent.parent / "scripts" / "changelog.py"
)
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


def test_to_entries_parses_versions_and_strips_markdown():
    md = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n### Added\n- not released yet\n\n"
        "## [0.2.0] - 2026-02-02\n\n"
        "### Added\n- **Bold thing** with `code` and a [link](https://x).\n"
        "  continues on the next line\n"
        "### Fixed\n- plain fix\n\n"
        "## [0.1.0] - 2026-01-01\n\n### Added\n- first\n"
    )
    entries = changelog.to_entries(md)
    assert [e["version"] for e in entries] == ["v0.2.0", "v0.1.0"]  # Unreleased skipped, newest-first
    e = entries[0]
    assert e["date"] == "2026-02-02"
    assert e["changes"] == [
        "Bold thing with code and a link. continues on the next line",  # md stripped, continuation joined
        "plain fix",
    ]


def test_json_output_matches_committed_changelog():
    """The committed marketing changelog.json must be the generator's output for the
    current CHANGELOG.md — guards against it drifting stale again."""
    import json

    expected = changelog.to_entries(changelog.CHANGELOG.read_text(encoding="utf-8"))
    actual = json.loads(changelog.MARKETING_JSON.read_text(encoding="utf-8"))
    assert actual == expected, "run `python scripts/changelog.py json` to resync"
