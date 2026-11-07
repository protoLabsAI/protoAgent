"""ADR 0125 P2-D2e — knowledge-package docstrings/comments use the new tool
names and describe the memories/chunks split.

A text-only guard (no behavior under test): the four knowledge modules must
not reference the retired memory tool names, and their module docstrings must
reflect that ``chunks`` now holds knowledge while agent memory lives in the
``memories`` table. Reads the raw source so comments (not just ``__doc__``
docstrings) are covered too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The four files this card sweeps.
SWEPT_FILES = (
    REPO / "src" / "knowledge" / "__init__.py",
    REPO / "src" / "knowledge" / "store.py",
    REPO / "src" / "knowledge" / "trust.py",
    REPO / "src" / "knowledge" / "layered.py",
)

# Retired names (ADR 0125 D3 hard rename) that must not survive in these files.
RETIRED_NAMES = ("memory_ingest", "memory_recall", "forget_memory", "memory_stats")


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", SWEPT_FILES, ids=lambda p: p.name)
def test_file_exists(path: Path) -> None:
    assert path.is_file(), f"expected swept file to exist: {path}"


@pytest.mark.parametrize("path", SWEPT_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("name", RETIRED_NAMES)
def test_no_retired_tool_names(path: Path, name: str) -> None:
    assert name not in _text(path), f"{path.name} still mentions retired name {name!r}"


def test_package_docstring_names_the_split() -> None:
    doc = _text(REPO / "src" / "knowledge" / "__init__.py")
    # Chunks are framed as knowledge, memory is pointed at the memories table.
    assert "memories" in doc and "knowledge/memories.py" in doc
    assert "knowledge_search" in doc


def test_store_docstring_points_memory_at_memories_table() -> None:
    doc = _text(REPO / "src" / "knowledge" / "store.py")
    assert "memories" in doc and "knowledge/memories.py" in doc
    # The chunk write path is the knowledge tool, not a memory tool.
    assert "knowledge_ingest" in doc


def test_trust_conversation_tier_value_unchanged() -> None:
    from knowledge.trust import TRUST_TIERS, trust_tier

    # The card only renamed the comment; the tier value stays agent-tier (2).
    assert TRUST_TIERS["conversation"] == 2
    assert trust_tier("conversation") == 2
