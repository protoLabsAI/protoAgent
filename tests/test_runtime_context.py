"""Runtime context contract (ADR 0033 slice 2) — cacheable prefix + volatile delta."""

from __future__ import annotations

import types

from runtime.context import AssembledContext, ContextAssembler, assemble_context


class _FakeStore:
    def search(self, query, k=5):
        return [{"table": "finding", "preview": f"hit for {query}"}]


class _FakeSkills:
    """Stub matching the always-on index surface (ADR 0060)."""

    def skill_summaries(self, limit=None):
        rows = [{"name": "deploy", "description": "how to deploy", "slash": ""}]
        return rows[:limit] if limit is not None else rows

    def discoverable_count(self):
        return 1


def _cfg():
    return types.SimpleNamespace(knowledge_top_k=5, skills_top_k=5)


def test_stable_prefix_is_turn_stable_for_caching():
    a = assemble_context(_cfg(), query="alpha")
    b = assemble_context(_cfg(), query="beta totally different")
    # The prefix (persona/system prompt) must be byte-identical across turns — cacheable.
    assert a.stable_prefix and a.stable_prefix == b.stable_prefix


def test_volatile_delta_reflects_retrieval_and_query():
    ctx = assemble_context(_cfg(), query="ship it", knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    assert "hit for ship it" in ctx.volatile_delta  # knowledge block, query-bound
    assert "deploy: how to deploy" in ctx.volatile_delta  # skills block
    assert any(s.startswith("knowledge:") for s in ctx.sources)
    assert any(s.startswith("skills:") for s in ctx.sources)


def test_no_query_means_no_knowledge_but_skills_still_listed():
    """Knowledge is query-bound (no query → no knowledge); the skill index is
    always-on (ADR 0060), so it appears regardless of the query."""
    ctx = assemble_context(_cfg(), query="", knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    assert "hit for" not in ctx.volatile_delta
    assert "deploy: how to deploy" in ctx.volatile_delta


def test_as_prompt_keeps_prefix_first_then_volatile_then_message():
    ctx = AssembledContext(stable_prefix="PERSONA", volatile_delta="KB", sources=[])
    prompt = ctx.as_prompt("do the thing")
    assert prompt == "PERSONA\n\nKB\n\ndo the thing"
    # prefix stays at the front + intact (so a backend can cache just it)
    assert prompt.startswith("PERSONA")


def test_assembler_object_implements_the_contract():
    asm = ContextAssembler(config=_cfg(), knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    ctx = asm.assemble(query="x")
    assert ctx.stable_prefix and "hit for x" in ctx.volatile_delta
    asm.after_turn(user="x", response="y")  # no-op in slice 2, must not raise
