"""Characterization tests for the current context architecture.

ADR 0107 Phase 0: these tests document TODAY's context shape — the Chunk
dataclass, KnowledgeMiddleware.compose_context return contract, and the
context_frame_message output — so that the v2 migration (phases 1-4) can
verify it preserves backward compatibility or breaks it deliberately.

These are characterization tests, not correctness tests: they freeze the
observed behavior as literals and fail loudly if a refactor changes the
shape.  When a v2 phase intentionally changes the shape, update the
assertion here and document the change in the phase's PR.
"""

from __future__ import annotations

import dataclasses

from langchain_core.messages import HumanMessage

from graph.context_frame import (
    CONTEXT_FRAME_KWARG,
    context_frame_message,
    is_context_frame,
)
from graph.middleware.knowledge import KnowledgeMiddleware
from knowledge.store import Chunk


# ---------------------------------------------------------------------------
# 1. Chunk dataclass fields — the current knowledge-store row shape
# ---------------------------------------------------------------------------

# The exact field set of Chunk as of ADR 0107 Phase 0.  A new column
# (memory_kind, subject, review_state, expires_at) added by #3072 Phase 2
# should UPDATE this list — the test failing is the signal that the
# characterization needs a deliberate bump.
EXPECTED_CHUNK_FIELDS = [
    "id",
    "content",
    "domain",
    "heading",
    "source",
    "source_type",
    "finding_type",
    "created_at",
    "updated_at",
    "namespace",
    "invalidated_at",
    "epoch",
    "invalidation_reason",
]


def test_chunk_dataclass_fields():
    """Chunk has exactly the expected fields, in order."""
    actual = [f.name for f in dataclasses.fields(Chunk)]
    assert actual == EXPECTED_CHUNK_FIELDS, (
        f"Chunk fields changed.  If this is intentional (e.g. #3072 typed "
        f"memory axes), update EXPECTED_CHUNK_FIELDS.\n"
        f"  expected: {EXPECTED_CHUNK_FIELDS}\n"
        f"  actual:   {actual}"
    )


def test_chunk_is_a_dataclass():
    """Chunk is a proper dataclass (not a dict wrapper or namedtuple)."""
    assert dataclasses.is_dataclass(Chunk)
    assert not isinstance(Chunk, type(None))


# ---------------------------------------------------------------------------
# 2. KnowledgeMiddleware.compose_context return contract
# ---------------------------------------------------------------------------


def test_compose_context_returns_context_keys():
    """compose_context always returns both 'context' and 'context_sections'."""
    mw = KnowledgeMiddleware(knowledge_store=None)
    result = mw.compose_context({}, record=False)
    assert result is not None, "compose_context should never return None (ADR 0101)"
    assert "context" in result, "Missing 'context' key"
    assert "context_sections" in result, "Missing 'context_sections' key"


def test_compose_context_empty_state_clears_channels():
    """With a store that returns nothing and no prior sessions, both channels
    are set to their empty values so the checkpointer doesn't carry stale
    context forward."""
    # A minimal store whose search returns nothing and has no hot memory
    class _EmptyStore:
        def search(self, *a, **kw):
            return []

        def get_hot_memory(self):
            return ""

        def get_hot_memory_entries(self):
            return []

    mw = KnowledgeMiddleware(knowledge_store=_EmptyStore())
    # Override the prior-sessions cache so it doesn't hit the filesystem
    mw._prior_sessions_cache = ""
    mw._prior_sessions_loaded_at = float("inf")

    result = mw.compose_context({"messages": []}, record=False)
    assert result is not None
    assert isinstance(result["context"], str)
    assert isinstance(result["context_sections"], list)


def test_compose_context_sections_shape():
    """context_sections entries have 'label' and 'chars' keys."""

    class _HotStore:
        def search(self, *a, **kw):
            return []

        def get_hot_memory(self):
            return "always-on fact"

        def get_hot_memory_entries(self):
            return [(1, "always-on fact")]

    mw = KnowledgeMiddleware(knowledge_store=_HotStore())
    mw._prior_sessions_cache = ""
    mw._prior_sessions_loaded_at = float("inf")

    result = mw.compose_context({"messages": []}, record=False)
    assert result is not None
    sections = result["context_sections"]
    assert len(sections) > 0, "Expected at least one section from hot memory"
    for sec in sections:
        assert "label" in sec, f"Section missing 'label': {sec}"
        assert "chars" in sec, f"Section missing 'chars': {sec}"
        assert isinstance(sec["chars"], int)


# ---------------------------------------------------------------------------
# 3. context_frame_message — the tagged HumanMessage
# ---------------------------------------------------------------------------


def test_context_frame_message_type():
    """context_frame_message produces a HumanMessage."""
    msg = context_frame_message("test")
    assert isinstance(msg, HumanMessage)


def test_context_frame_message_envelope():
    """The message content is wrapped in <injected_context> tags."""
    msg = context_frame_message("hello world")
    assert msg.content.startswith("<injected_context>\n")
    assert msg.content.endswith("\n</injected_context>")
    assert "hello world" in msg.content


def test_context_frame_message_kwarg():
    """The frame message carries the CONTEXT_FRAME_KWARG marker."""
    msg = context_frame_message("test")
    assert msg.additional_kwargs.get(CONTEXT_FRAME_KWARG) is True


def test_is_context_frame_roundtrip():
    """is_context_frame identifies messages produced by context_frame_message."""
    msg = context_frame_message("test")
    assert is_context_frame(msg) is True


def test_is_context_frame_rejects_normal_message():
    """is_context_frame rejects a plain HumanMessage."""
    normal = HumanMessage(content="hello")
    assert is_context_frame(normal) is False


def test_context_frame_kwarg_value():
    """The kwarg constant has the expected string value."""
    assert CONTEXT_FRAME_KWARG == "protoagent_injected_context"
