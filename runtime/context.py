"""Runtime context contract (ADR 0033, slice 2).

Two planes reach any brain: the **tool plane** (the operator MCP bus, slice 1) and the
**context plane** — the *injected* stuff (persona, retrieved knowledge, skills, prior
sessions). This module is the context plane's contract, so context is produced one way and
consumed by any runtime (native LangGraph today, an ACP coding agent in slice 3).

Caching discipline (ADR 0033 D5): the **stable prefix** (persona + static instructions) is
byte-identical turn to turn — cache it. The **volatile delta** (what's retrieved for *this*
turn) goes after it and never mutates the prefix. The native runtime satisfies this via
middleware (`build_system_prompt` for the prefix, `KnowledgeMiddleware` for the delta); the
ACP runtime calls `assemble_context()` to build its prompt + `after_turn()` to write back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class AssembledContext:
    """The two halves of a turn's context, kept apart so the prefix stays cacheable."""

    stable_prefix: str  # persona + static instructions — turn-stable, cache it
    volatile_delta: str = ""  # knowledge/skills/prior-sessions retrieved for THIS turn
    sources: list[str] = field(default_factory=list)  # what fed the delta (telemetry/debug)

    def as_prompt(self, message: str) -> str:
        """One-shot composition: prefix, then volatile, then the turn's message.

        The prefix stays first + intact so a backend can mark just it for prompt caching.
        """
        parts = [self.stable_prefix]
        if self.volatile_delta:
            parts.append(self.volatile_delta)
        if message:
            parts.append(message)
        return "\n\n".join(p for p in parts if p)


class RuntimeContext(Protocol):
    """What every runtime implements so the rest of the system is runtime-agnostic."""

    def assemble(self, *, query: str = "") -> AssembledContext: ...
    def after_turn(self, *, user: str = "", response: str = "") -> None: ...


def build_stable_prefix(config=None, *, include_subagents: bool = True) -> str:
    """The cacheable persona + static instructions — the system prompt. Turn-stable.

    Reuses `graph.prompts.build_system_prompt` (reads SOUL) so the native loop and any
    external runtime share one persona — no drift.
    """
    from graph.prompts import build_system_prompt

    return build_system_prompt(include_subagents=include_subagents)


def _format_skills(skills) -> str:
    lines = ["[Relevant learned skills:]"]
    for s in skills:
        name = getattr(s, "name", "skill")
        desc = getattr(s, "description", "") or ""
        lines.append(f"- {name}: {desc}".rstrip())
    return "\n".join(lines)


def _format_knowledge(results) -> str:
    # Mirrors KnowledgeMiddleware's block ({table, preview}) so an external brain sees
    # exactly what the native loop would inject.
    lines = ["[Relevant knowledge from previous sessions:]"]
    for r in results:
        lines.append(f"- [{r.get('table', '')}] {r.get('preview', '')}")
    return "\n".join(lines)


def retrieve_volatile(
    config=None, *, query: str = "", knowledge_store=None, skills_index=None, memory_path: str = "/sandbox/memory/"
):
    """The per-turn context blocks (prior sessions + skills + knowledge). Never raises.

    Same stores + query as `KnowledgeMiddleware`, so an external brain is fed what the
    native loop would inject. Returns ``(delta_text, sources)``.
    """
    blocks: list[str] = []
    sources: list[str] = []
    q = (query or "").strip()

    try:
        from graph.middleware.memory import load_prior_sessions

        prior = load_prior_sessions(memory_path)
        if prior:
            blocks.append(prior)
            sources.append("prior_sessions")
    except Exception:  # noqa: BLE001
        log.debug("[context] prior-sessions load skipped", exc_info=True)

    if skills_index is not None and q:
        try:
            k = int(getattr(config, "skills_top_k", 5) or 5)
            skills = skills_index.load_skills(q, k=k)
            if skills:
                blocks.append(_format_skills(skills))
                sources.append(f"skills:{len(skills)}")
        except Exception:  # noqa: BLE001
            log.warning("[context] skills retrieval failed", exc_info=True)

    if knowledge_store is not None and q:
        try:
            k = int(getattr(config, "knowledge_top_k", 5) or 5)
            results = knowledge_store.search(q, k=k)
            if results:
                blocks.append(_format_knowledge(results))
                sources.append(f"knowledge:{len(results)}")
        except Exception:  # noqa: BLE001
            log.warning("[context] knowledge retrieval failed", exc_info=True)

    return "\n\n".join(blocks), sources


def assemble_context(
    config=None, *, query: str = "", knowledge_store=None, skills_index=None, include_subagents: bool = True
) -> AssembledContext:
    """Build a turn's context as a cacheable prefix + a volatile delta (ADR 0033 D4)."""
    prefix = build_stable_prefix(config, include_subagents=include_subagents)
    delta, sources = retrieve_volatile(
        config,
        query=query,
        knowledge_store=knowledge_store,
        skills_index=skills_index,
    )
    return AssembledContext(stable_prefix=prefix, volatile_delta=delta, sources=sources)


def after_turn(knowledge_store=None, *, user: str = "", response: str = "") -> None:
    """Durable write-back hook (ADR 0033 D5).

    The native loop does fact write-back via the knowledge-ingest middleware. The ACP
    runtime (slice 3) calls this after a turn. Intentionally a no-op in slice 2 — the
    read side (`assemble_context`) is what unblocks the ACP runtime; fact extraction is
    wired where the turn result is available (slice 3).
    """
    return None


@dataclass
class ContextAssembler:
    """A concrete `RuntimeContext` bound to stores — what a runtime holds + calls."""

    config: object = None
    knowledge_store: object = None
    skills_index: object = None
    include_subagents: bool = True

    def assemble(self, *, query: str = "") -> AssembledContext:
        return assemble_context(
            self.config,
            query=query,
            knowledge_store=self.knowledge_store,
            skills_index=self.skills_index,
            include_subagents=self.include_subagents,
        )

    def after_turn(self, *, user: str = "", response: str = "") -> None:
        after_turn(self.knowledge_store, user=user, response=response)
