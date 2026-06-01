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
