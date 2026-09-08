"""DelegateRegistry — parse the ``delegates`` config into dispatchable targets.

Rebuilt from config on every graph build / hot-reload (ADR 0025), so editing the
``delegates`` section + Save & Reload swaps the roster live — protoAgent's native
equivalent of ORBIS's ``registry.reload()`` + session refresh.
"""

from __future__ import annotations

import asyncio
import logging

from . import status
from .adapters import ADAPTERS, Delegate, DelegateError

logger = logging.getLogger("protoagent.plugins.delegates")

# The delegate types that HAVE a continuing conversation to select (#3360): an ``acp``
# coding agent keeps a session, an ``a2a`` peer groups messages under a ``contextId`` it
# assigns. ``openai`` is deliberately absent — that adapter posts to a stateless chat
# endpoint, so a conversation key would name nothing and silently accepting one would
# promise continuity the wire cannot deliver.
_CONVERSATIONAL_TYPES = ("acp", "a2a")


class DelegateRegistry:
    def __init__(self, raw_delegates: list | None = None):
        self._items: dict[str, Delegate] = {}
        for raw in raw_delegates or []:
            self._add(raw)

    def _add(self, raw) -> None:
        if not isinstance(raw, dict):
            logger.warning("[delegates] ignoring non-mapping entry: %r", raw)
            return
        dtype = str(raw.get("type", "")).strip()
        adapter = ADAPTERS.get(dtype)
        if adapter is None:
            logger.warning(
                "[delegates] %s: unknown type %r (want one of %s) — skipped",
                raw.get("name"),
                dtype,
                ", ".join(ADAPTERS),
            )
            return
        try:
            d = adapter.parse(raw)
        except DelegateError as exc:
            logger.warning("[delegates] dropping invalid delegate: %s", exc)
            return
        if d.name in self._items:
            logger.warning("[delegates] duplicate name %r — keeping first", d.name)
            return
        self._items[d.name] = d

    def names(self) -> list[str]:
        return list(self._items)

    def get(self, name: str) -> Delegate | None:
        return self._items.get(name)

    def listing(self) -> str:
        """Human/LLM-facing one-liner per delegate (for the tool description)."""
        return "; ".join(
            f"`{d.name}` ({d.type}{' — ' + d.description if d.description else ''})" for d in self._items.values()
        )

    def roster(self) -> list[dict]:
        """Structured one-entry-per-delegate roster (for the ``list_agents`` tool)."""
        return [
            {"name": d.name, "type": d.type, "description": d.description, "url": d.url} for d in self._items.values()
        ]

    async def dispatch(
        self,
        name: str,
        query: str,
        *,
        item_id: str | None = None,
        raw: bool = False,
        resume_task_id: str | None = None,
        conversation_key: str | None = None,
        origin_session_id: str | None = None,
        permissions: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Dispatch ``query`` to the named delegate.

        ``item_id`` is the work-item identity for adapters that manage a git
        lifecycle (ADR 0076); ``resume_task_id`` answers a PARKED a2a task (the
        HITL delegation chain). ``timeout`` overrides the delegate's configured
        timeout for THIS call only (seconds) — ``None`` keeps the configured one.
        ``raw=True`` bypasses
        the managed-git lifecycle for programmatic callers that consume the reply as
        DATA (e.g. the coder ladder's candidate generation, ADR 0064) — no branch, no
        commit, no PR, no claim; just the coder's text. ``conversation_key`` names ONE
        continuing conversation with the delegate without mutating the configured
        roster — a persistent ACP session, or (#3360) the A2A ``contextId`` an ``a2a``
        peer assigned that key, so repeated addresses from one chat thread land in one
        peer-side conversation instead of N unrelated ones.
        ``origin_session_id`` is the chat SESSION this dispatch came from, recorded beside
        the resolved ``conversation_key`` so a delete that knows only the session can forget
        the a2a context later (#3362). Explicit when a caller knows it; the room path can't
        pass it through the host-free ``graph/mention_op``, so it falls back to the session
        bound by ``recording_session`` (a ContextVar). Blank when neither supplies one.
        ``permissions`` is a per-call ACP ceiling; currently only ``readonly`` is
        accepted, and delegate types that cannot enforce it are refused."""
        from . import conversations

        d = self._items.get(name)
        if d is None:
            raise DelegateError(f"unknown delegate {name!r}. Configured: {', '.join(self._items) or '(none)'}.")
        conversation_key = str(conversation_key or "").strip()
        # Explicit wins; otherwise the room's ``recording_session`` block bound one on a
        # ContextVar because it reaches here through host-free code that can't carry it.
        origin_session_id = str(origin_session_id or "").strip() or conversations.current_origin_session()
        permissions = str(permissions or "").strip().lower()
        if conversation_key and d.type not in _CONVERSATIONAL_TYPES:
            raise DelegateError(
                f"delegate {name!r} is type {d.type!r} — conversation_key needs something on the "
                "other side to continue: an acp session, or the A2A contextId an a2a peer assigns. "
                "The type that has neither today is openai: an openai-compat delegate posts to a "
                "stateless chat endpoint, every call is a fresh completion with no server-side "
                "conversation to resume, so there is nothing for a key to select. Send the context "
                "in the query instead."
            )
        if permissions and permissions != "readonly":
            raise DelegateError("permissions must be 'readonly' when an invocation ceiling is requested.")
        if permissions and d.type != "acp":
            raise DelegateError(f"delegate {name!r} is type {d.type!r} and cannot enforce a permissions ceiling.")
        if conversation_key or origin_session_id or permissions or (raw and d.manage_git):
            import dataclasses

            d = dataclasses.replace(
                d,
                conversation_key=conversation_key,
                # Rides beside the resolved key so an answered a2a exchange records it
                # (adapters ``_learn`` → ``conversations.remember(session_id=…)``, #3362).
                origin_session_id=origin_session_id,
                permissions_ceiling=permissions,
                # A read-only invocation ceiling covers host-managed side effects
                # too, not only ACP permission requests from the child.
                manage_git=False if permissions or raw else d.manage_git,
            )
        # Only a2a tasks can park-and-resume. Silently ignoring a resume id on any
        # other adapter would run the ANSWER as a brand-new task (for a managed-git
        # coder: a brand-new PR) — refuse instead.
        if resume_task_id and d.type != "a2a":
            raise DelegateError(
                f"delegate {name!r} is type {d.type!r} — resume_task_id only applies to a2a "
                "delegates (only they park tasks on questions). Did you mean the delegate "
                "that asked the question?"
            )
        # Record the outcome HERE — the one funnel every caller goes through (the
        # `delegate_to` tool, its background variant, the coder ladder, the board loop),
        # so the panel's picture doesn't depend on which surface triggered the dispatch.
        # A cancellation is deliberately not a failure: an operator stopping a turn says
        # nothing about the delegate (see status.py).
        # The DURABLE half of the same funnel. `status` is an in-memory last-outcome
        # cache for the panel's dot; the ledger is the record that survives the process
        # and answers "what work actually flowed along this edge" (graph/ledger.py). Both
        # sit here for the same reason the comment above gives: this is the one funnel,
        # so neither depends on which surface triggered the dispatch.
        #
        # Reached through graph.sdk rather than imported directly — a plugin may never
        # import `server`, and the SDK is the seam that keeps this side of the layering
        # contract honest.
        from graph import ledger

        with ledger.dispatch(
            to_kind=d.type,
            to_name=d.name,
            to_instance=str(getattr(d, "url", "") or ""),
            what=query,
            session_id=origin_session_id or "",
            origin="delegate_to",
        ):
            try:
                reply = await ADAPTERS[d.type].dispatch(
                    d, query, timeout=timeout, item_id=item_id, resume_task_id=resume_task_id
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status.record_failure(d.name, str(exc) or type(exc).__name__)
                raise
            status.record_success(d.name)
            return reply

    def recording_session(self, session_id: str):
        """Context manager binding the originating chat session for the a2a continuity that
        delegations dispatched inside it record (#3362).

        The chat-room dispatch boundary opens it — the server ``@`` dispatch and
        ``delegate_to``'s room helper — and ``dispatch`` reads it (via ``conversations``)
        to fill ``remember``'s ``session_id``, storing the origin BESIDE the resolved
        conversation key. It rides a ContextVar, not a dispatch argument, because both
        boundaries reach ``dispatch`` through ``graph/mention_op``, which is host-free and
        never imports this plugin. Core reaches this duck-typed through
        ``STATE.delegate_registry`` — the same seam as ``forget_conversation`` — so a fork
        without it (or a blank session) simply records no origin.
        """
        from . import conversations

        return conversations.origin_session(str(session_id or ""))

    def forget_conversation(self, conversation_key: str) -> int:
        """Drop the transport continuity this process holds for one conversation (#3360).

        The seam a thread-lifecycle event calls when this side's history is rewritten —
        ``server.chat.forget_delegate_conversations`` wires the rewind, delete and fork
        gestures to it through ``STATE.delegate_registry``, so core never imports this
        plugin. Returns how many peer contexts were dropped, and never raises: a cleanup
        path must not be able to fail the gesture it is cleaning up after.

        **Scope: the A2A ``contextId`` map, and only that.** A persistent ACP session is
        *not* torn down here, deliberately — it is a pooled subprocess holding a coding
        agent's live working state (``plugins/coding_agent._client_for``), so ending one
        is an operator-visible action with its own consequences, and it has behaved this
        way since ``conversation_key`` existed rather than being something #3360
        introduced. Widening this to ACP is a separate decision, not a follow-through.
        """
        from . import conversations

        try:
            return conversations.forget(str(conversation_key or ""))
        except Exception:  # noqa: BLE001 — best-effort cleanup, never the caller's problem
            logger.exception("[delegates] forgetting conversation %r failed", conversation_key)
            return 0

    def forget_conversations_for_session(self, session_id: str) -> int:
        """Drop the transport continuity this process holds for one CHAT SESSION (#3362).

        The origin-scoped companion to ``forget_conversation``: that one takes the resolved
        conversation KEY a room dispatched under (which rewind/fork know, because they
        resolve it); this one takes the chat SESSION id, and drops every remembered A2A
        context whose recorded origin is that session. It never infers membership from the
        *shape* of a resolved key — a custom thread-id resolver (ADR 0029 §D4 / #571) can
        mint one to anything and the map is one-way — so a caller that knows only the
        session can still reach every context it minted.

        Same contract as ``forget_conversation``: best-effort, never raises, returns how
        many were dropped. Scoped to the A2A ``contextId`` map only; a persistent ACP
        session is not torn down here, for the reasons ``forget_conversation`` gives. No
        route calls this yet — the DELETE wiring lands in a following slice.
        """
        from . import conversations

        try:
            return conversations.forget_by_session(str(session_id or ""))
        except Exception:  # noqa: BLE001 — best-effort cleanup, never the caller's problem
            logger.exception("[delegates] forgetting session %r conversations failed", session_id)
            return 0
