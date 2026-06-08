"""Runtime context contract (ADR 0033 slice 2) — cacheable prefix + volatile delta."""

from __future__ import annotations

import types

from runtime.context import AssembledContext, ContextAssembler, assemble_context


class _FakeStore:
    def search(self, query, k=5):
        return [{"table": "finding", "preview": f"hit for {query}"}]


class _FakeSkill:
    def __init__(self, name, desc):
        self.name = name
        self.description = desc


class _FakeSkills:
    def load_skills(self, query, k=5):
        return [_FakeSkill("deploy", "how to deploy")]


def _cfg():
    return types.SimpleNamespace(knowledge_top_k=5, skills_top_k=5)


def test_stable_prefix_is_turn_stable_for_caching():
    a = assemble_context(_cfg(), query="alpha")
    b = assemble_context(_cfg(), query="beta totally different")
    # The prefix (persona/system prompt) must be byte-identical across turns — cacheable.
    assert a.stable_prefix and a.stable_prefix == b.stable_prefix


def test_volatile_delta_reflects_retrieval_and_query():
    ctx = assemble_context(_cfg(), query="ship it", knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    assert "hit for ship it" in ctx.volatile_delta          # knowledge block, query-bound
    assert "deploy: how to deploy" in ctx.volatile_delta     # skills block
    assert any(s.startswith("knowledge:") for s in ctx.sources)
    assert any(s.startswith("skills:") for s in ctx.sources)


def test_no_query_means_no_knowledge_or_skills():
    ctx = assemble_context(_cfg(), query="", knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    assert "hit for" not in ctx.volatile_delta
    assert "deploy" not in ctx.volatile_delta


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
