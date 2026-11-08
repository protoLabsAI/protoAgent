"""ADR 0125 D3 renamed the memory tools with no aliases, so a bundled skill or SOUL preset
that still names an old tool tells the model to call a tool that doesn't exist."""

from pathlib import Path

import pytest

_REMOVED = ("memory_ingest", "memory_recall", "forget_memory", "memory_stats")
_ROOT = Path(__file__).resolve().parent.parent
_FILES = sorted(
    p
    for d in ("config/skills", "config/soul-presets")
    for p in (_ROOT / d).rglob("*.md")
)


def test_bundled_config_files_found():
    assert _FILES


@pytest.mark.parametrize("path", _FILES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_bundled_config_names_no_removed_memory_tool(path):
    text = path.read_text(encoding="utf-8")
    assert not [name for name in _REMOVED if name in text]
