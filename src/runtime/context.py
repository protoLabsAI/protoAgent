"""Runtime context contract (ADR 0033, slice 2; ADR 0108 D8).

Two planes reach any brain: the **tool plane** (the operator MCP bus, slice 1) and the
**context plane** — the *injected* stuff (persona, retrieved knowledge, skills, prior
sessions, the agent's own working state). This module is the context plane's contract,
so context is produced one way and consumed by any runtime (native LangGraph today, an
ACP coding agent in slice 3).

Caching discipline (ADR 0033 D5): the **stable prefix** (persona + static instructions) is
byte-identical turn to turn — cache it. The **volatile delta** (what's retrieved for *this*
turn) goes after it and never mutates the prefix. Both halves are shared with the native
loop: `build_system_prompt` composes the prefix for every runtime, and the delta is the same
:func:`graph.projection.compose_projected_context` the native `KnowledgeMiddleware` delivers
(ADR 0108 D8) — one projection, so an external brain sees the `<injected_memory>` envelope,
hot memory, trust-ranked RAG hits, the budgeted skill index, and `<working_state>` exactly
as the native loop does, with the incognito rule and the injection log applied identically.
The ACP runtime calls `assemble_context()` to build its prompt + `after_turn()` to write back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class AssembledContext:
    """The two halves of a turn's context, kept apart so the prefix stays cacheable."""

    stable_prefix: str  # persona + static instructions — turn-stable, cache it
    volatile_delta: str = ""  # knowledge/skills/prior-sessions/working-state projected for THIS turn
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


def build_stable_prefix(
    config=None,
    *,
    include_subagents: bool = True,
    bound_tool_names: frozenset[str] | None = None,
    projects: list[dict] | None = None,
) -> str:
    """The cacheable persona + static instructions — the system prompt. Turn-stable.

    Reuses `graph.prompts.build_system_prompt` (reads SOUL) so the native loop and any
    external runtime share one persona — no drift. Same inputs ⇒ byte-equal prompt.

    ``bound_tool_names`` (#3190): when provided, capability-specific operating-model
    sections are generated only for tools that are actually bound. None emits everything
    unconditionally (legacy behavior). ``projects`` (ADR 0007) names the managed project
    workspaces exactly as ``graph/agent.py`` does for the native loop — pass it only when
    the runtime's tool plane really carries the fenced filesystem tools, or the section
    describes tools the brain cannot call.
    """
    from graph.prompts import build_system_prompt

    return build_system_prompt(
        include_subagents=include_subagents,
        projects=projects,
        bound_tool_names=bound_tool_names,
    )


def assemble_context(
    config=None,
    *,
    query: str = "",
    knowledge_store=None,
    skills_index=None,
    include_subagents: bool = True,
    bound_tool_names: frozenset[str] | None = None,
    projects: list[dict] | None = None,
    state: dict | None = None,
    incognito: bool = False,
    record: bool = False,
) -> AssembledContext:
    """Build a turn's context as a cacheable prefix + a volatile delta (ADR 0033 D4).

    The delta is the shared projection (ADR 0108 D8): ``query`` is the retrieval key
    (empty → no RAG search; the skill index and working state are query-independent),
    ``state`` carries an optional ``session_id`` for working-state / injection-log
    attribution, ``incognito`` suppresses every memory part (ADR 0069 D3b). The delivery
    knobs (top-k, namespace scope, trust floor, skill-index caps) are read off ``config``
    exactly as the native middleware is wired, so both runtimes follow one policy.

    ``record`` defaults to False here: a bare call is a composition, not evidence that a
    model call happened, and the injection log (ADR 0069 D6) must never be fabricated.
    :class:`ContextAssembler` — what a runtime holds for real turns — records by default.
    """
    from graph.projection import ProjectionOptions, compose_projected_context

    prefix = build_stable_prefix(
        config,
        include_subagents=include_subagents,
        bound_tool_names=bound_tool_names,
        projects=projects,
    )
    projected = compose_projected_context(
        query or "",
        knowledge_store,
        skills_index,
        state or {},
        incognito=incognito,
        record=record,
        # No per-turn model here: an external runtime's model is fixed for the
        # session (there is no per-chat override to follow), so the knobs stay
        # sized off the configured default — the native path passes the turn's
        # ``state["model"]`` instead (ADR 0108 D6).
        options=ProjectionOptions.from_config(config),
    )
    return AssembledContext(stable_prefix=prefix, volatile_delta=projected.text, sources=list(projected.sources))


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
    """A concrete `RuntimeContext` bound to stores — what a runtime holds + calls.

    ``record`` is True here (unlike the bare :func:`assemble_context`): an assembler is
    bound to a runtime's real turns, so what it composes did enter a model call and
    belongs in the injection log (ADR 0069 D6) — but only when the turn is attributable.
    A row with an empty ``session_id`` answers nobody's "what entered THIS turn?", so a
    call records only when a session id is known: ``session_id`` on the assembler (the
    runtime's thread) or per call. No id → composed and delivered, never recorded.
    Set ``record=False`` for a speculative assembler.

    The prefix is honest about the runtime's tool plane (#3190): ``include_subagents``
    and ``bound_tool_names`` describe what the brain can actually call, and ``projects``
    is passed only by a runtime whose tool plane carries the fenced filesystem tools.
    When the bound set is knowable only once the stores behind it are booted, give
    ``bound_tool_names_factory`` instead: it is resolved ONCE, at the first ``assemble()``
    (a real turn — the stores are up by then), and cached. A factory that fails leaves
    the legacy ``None`` (full doctrine) with a warning — never a guessed set.
    """

    config: object = None
    knowledge_store: object = None
    skills_index: object = None
    include_subagents: bool = True
    bound_tool_names: frozenset[str] | None = None
    projects: list[dict] | None = None
    record: bool = True
    session_id: str | None = None
    bound_tool_names_factory: Callable[[], frozenset[str] | None] | None = None
    _bound_resolved: bool = field(default=False, init=False, repr=False, compare=False)

    def _resolve_bound_tool_names(self) -> frozenset[str] | None:
        if self.bound_tool_names is None and self.bound_tool_names_factory is not None and not self._bound_resolved:
            self._bound_resolved = True  # one attempt: a broken factory must not re-raise every turn
            try:
                names = self.bound_tool_names_factory()
                self.bound_tool_names = frozenset(names) if names is not None else None
            except Exception:  # noqa: BLE001 — the prefix must still build; None = legacy full doctrine
                log.warning("[context] bound-tool resolution failed; emitting the full doctrine", exc_info=True)
        return self.bound_tool_names

    def assemble(self, *, query: str = "", session_id: str | None = None) -> AssembledContext:
        sid = session_id or self.session_id
        return assemble_context(
            self.config,
            query=query,
            knowledge_store=self.knowledge_store,
            skills_index=self.skills_index,
            include_subagents=self.include_subagents,
            bound_tool_names=self._resolve_bound_tool_names(),
            projects=self.projects,
            state={"session_id": sid} if sid else {},
            record=bool(self.record and sid),  # attributable turns only — "" is not an id
        )

    def after_turn(self, *, user: str = "", response: str = "") -> None:
        after_turn(self.knowledge_store, user=user, response=response)
