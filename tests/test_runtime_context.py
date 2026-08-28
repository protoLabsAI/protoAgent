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
    # ADR 0108 D8: the delta is the NATIVE projection — RAG hits ride inside the
    # <injected_memory> envelope and the skill index is the <available_skills> block.
    ctx = assemble_context(_cfg(), query="ship it", knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    assert "<injected_memory>" in ctx.volatile_delta
    assert "hit for ship it" in ctx.volatile_delta  # knowledge block, query-bound
    assert '<skill name="deploy">how to deploy</skill>' in ctx.volatile_delta  # skills block
    assert any(s.startswith("knowledge:") for s in ctx.sources)
    assert any(s.startswith("skills:") for s in ctx.sources)


def test_no_query_means_no_knowledge_but_skills_still_listed():
    """Knowledge is query-bound (no query → no knowledge); the skill index is
    always-on (ADR 0060), so it appears regardless of the query."""
    ctx = assemble_context(_cfg(), query="", knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    assert "hit for" not in ctx.volatile_delta
    assert '<skill name="deploy">how to deploy</skill>' in ctx.volatile_delta


def test_skills_top_k_zero_lists_no_skills():
    """An explicit skills_top_k=0 means "list none" — it must NOT be coerced to the
    default 5 (the `or 5` bug). Matches the middleware path, which respects 0."""
    cfg = types.SimpleNamespace(knowledge_top_k=5, skills_top_k=0)
    ctx = assemble_context(cfg, query="x", knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    assert "deploy" not in ctx.volatile_delta
    assert not any(s.startswith("skills:") for s in ctx.sources)


def test_as_prompt_keeps_prefix_first_then_volatile_then_message():
    ctx = AssembledContext(stable_prefix="PERSONA", volatile_delta="KB", sources=[])
    prompt = ctx.as_prompt("do the thing")
    assert prompt == "PERSONA\n\nKB\n\ndo the thing"
    # prefix stays at the front + intact (so a backend can cache just it)
    assert prompt.startswith("PERSONA")


def test_build_stable_prefix_threads_bound_tool_names(monkeypatch):
    """build_stable_prefix must forward bound_tool_names to build_system_prompt
    so ACP runtimes get capability-derived prompts, not the full operating model.
    Regression for #3230."""
    from runtime.context import build_stable_prefix

    captured = {}

    def fake_build_system_prompt(**kwargs):
        captured.update(kwargs)
        return "prompt"

    monkeypatch.setattr("graph.prompts.build_system_prompt", fake_build_system_prompt)

    names = frozenset({"search", "read_file"})
    build_stable_prefix(include_subagents=False, bound_tool_names=names)

    assert captured.get("bound_tool_names") == names
    assert captured.get("include_subagents") is False


def test_assemble_context_threads_bound_tool_names(monkeypatch):
    """assemble_context must thread bound_tool_names to build_stable_prefix."""
    captured = {}

    def fake_prefix(config=None, *, include_subagents=True, bound_tool_names=None, projects=None):
        captured["bound_tool_names"] = bound_tool_names
        return "prefix"

    monkeypatch.setattr("runtime.context.build_stable_prefix", fake_prefix)

    names = frozenset({"task_create"})
    ctx = assemble_context(_cfg(), query="", bound_tool_names=names)
    assert captured["bound_tool_names"] == names
    assert ctx.stable_prefix == "prefix"


def test_context_assembler_threads_bound_tool_names(monkeypatch):
    """ContextAssembler must thread its bound_tool_names field through to
    assemble_context."""
    captured = {}
    original_assemble = assemble_context

    def spy_assemble(*args, **kwargs):
        captured.update(kwargs)
        return original_assemble(*args, **kwargs)

    monkeypatch.setattr("runtime.context.assemble_context", spy_assemble)

    names = frozenset({"search"})
    asm = ContextAssembler(config=_cfg(), bound_tool_names=names)
    asm.assemble(query="x")
    assert captured.get("bound_tool_names") == names


def test_assembler_object_implements_the_contract():
    asm = ContextAssembler(config=_cfg(), knowledge_store=_FakeStore(), skills_index=_FakeSkills())
    ctx = asm.assemble(query="x")
    assert ctx.stable_prefix and "hit for x" in ctx.volatile_delta
    asm.after_turn(user="x", response="y")  # no-op in slice 2, must not raise


# ── an honest prefix for external runtimes (#3190) ───────────────────────────────────


def test_build_stable_prefix_threads_projects(monkeypatch):
    """`projects` reaches build_system_prompt exactly like the native loop passes it."""
    from runtime.context import build_stable_prefix

    captured = {}

    def fake_build_system_prompt(**kw):
        captured.update(kw)
        return "P"

    monkeypatch.setattr("graph.prompts.build_system_prompt", fake_build_system_prompt)
    projects = [{"name": "api", "path": "/p/api", "write": True}]
    build_stable_prefix(include_subagents=False, bound_tool_names=frozenset(), projects=projects)
    assert captured["projects"] == projects
    assert captured["include_subagents"] is False and captured["bound_tool_names"] == frozenset()


def test_managed_projects_section_reaches_the_prefix():
    from runtime.context import build_stable_prefix

    prefix = build_stable_prefix(
        include_subagents=False,
        bound_tool_names=frozenset({"current_time"}),
        projects=[{"name": "api", "path": "/p/api", "write": True}],
    )
    assert "# Managed projects" in prefix and "**api** (read-write) — `/p/api`" in prefix
    bare = build_stable_prefix(include_subagents=False, bound_tool_names=frozenset({"current_time"}))
    assert "# Managed projects" not in bare


def test_assembler_threads_projects(monkeypatch):
    captured = {}

    def fake_prefix(config=None, *, include_subagents=True, bound_tool_names=None, projects=None):
        captured.update(include_subagents=include_subagents, bound_tool_names=bound_tool_names, projects=projects)
        return "P"

    monkeypatch.setattr("runtime.context.build_stable_prefix", fake_prefix)
    projects = [{"name": "api", "path": "/p/api", "write": False}]
    ContextAssembler(config=_cfg(), include_subagents=False, projects=projects).assemble(query="")
    assert captured == {"include_subagents": False, "bound_tool_names": None, "projects": projects}


def test_assembler_resolves_bound_tool_names_lazily_and_once(monkeypatch):
    """The exposed set is knowable only once the stores are booted, so it is resolved at
    the first assemble() — exactly once — and reaches the prefix builder."""
    seen = []

    def fake_prefix(config=None, *, include_subagents=True, bound_tool_names=None, projects=None):
        seen.append(bound_tool_names)
        return "P"

    monkeypatch.setattr("runtime.context.build_stable_prefix", fake_prefix)
    calls = []

    def factory():
        calls.append(1)
        return {"calculator", "current_time"}

    asm = ContextAssembler(config=_cfg(), bound_tool_names_factory=factory)
    assert asm.bound_tool_names is None  # nothing resolved at construction
    asm.assemble(query="")
    asm.assemble(query="again")
    assert calls == [1]
    assert asm.bound_tool_names == frozenset({"calculator", "current_time"})
    assert seen == [frozenset({"calculator", "current_time"})] * 2


def test_assembler_factory_failure_falls_back_to_full_doctrine(monkeypatch):
    """A factory that raises must not break the turn: the prefix builds with the legacy
    None (full doctrine), the failure is logged once, and it is not retried every turn."""
    seen = []

    def fake_prefix(config=None, *, include_subagents=True, bound_tool_names=None, projects=None):
        seen.append(bound_tool_names)
        return "P"

    monkeypatch.setattr("runtime.context.build_stable_prefix", fake_prefix)
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("stores not booted")

    asm = ContextAssembler(config=_cfg(), bound_tool_names_factory=broken)
    asm.assemble(query="")
    asm.assemble(query="")
    assert calls == [1] and seen == [None, None] and asm.bound_tool_names is None


def test_explicit_bound_tool_names_win_over_the_factory(monkeypatch):
    seen = []

    def fake_prefix(config=None, *, include_subagents=True, bound_tool_names=None, projects=None):
        seen.append(bound_tool_names)
        return "P"

    monkeypatch.setattr("runtime.context.build_stable_prefix", fake_prefix)
    asm = ContextAssembler(
        config=_cfg(), bound_tool_names=frozenset({"wait"}), bound_tool_names_factory=lambda: {"nope"}
    )
    asm.assemble(query="")
    assert seen == [frozenset({"wait"})]
