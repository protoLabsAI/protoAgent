"""WatchController — create/list/clear + the out-of-band evaluate/tick (ADR 0067).

Pure of graph calls so it's unit-testable. Verifiers are reused verbatim from
``graph/goals/verifiers.py``. On *met* the optional ``run_prompt`` is enqueued as a one-shot
agent turn via ``sdk.run_in_session`` (ADR/#1494) and ``on_met`` hooks fire; a passed
``deadline`` finishes the watch ``expired``; ``stall_after`` unchanged checks fire
``on_stalled`` (the watch stays active).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from graph.goals.verifiers import VERIFIERS, VerifyContext, run_verifier
from graph.watches.store import WatchStore
from graph.watches.types import Watch

log = logging.getLogger(__name__)

# Verifier types safe to create PROGRAMMATICALLY (agent tool / plugin / SDK). Only `plugin`
# — command/test/ci shell out and `data` eval()s a spec expr; those stay operator-only (the
# /api/watches channel, gated to operator-tier by the ADR 0066 path ceiling). Mirrors
# GoalController.SAFE_PROGRAMMATIC_VERIFIERS.
SAFE_PROGRAMMATIC_VERIFIERS = frozenset({"plugin"})


class WatchController:
    def __init__(self, config, store: WatchStore | None = None):
        self._config = config
        self._store = store or WatchStore()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, watch_id: str) -> asyncio.Lock:
        lock = self._locks.get(watch_id)
        if lock is None:
            lock = self._locks[watch_id] = asyncio.Lock()
        return lock

    @property
    def store(self) -> WatchStore:
        return self._store

    def active_watches(self) -> list[Watch]:
        return [w for w in self._store.all() if w.active]

    def list_watches(self) -> list[Watch]:
        return self._store.all()

    def clear(self, watch_id: str) -> bool:
        self._locks.pop(watch_id, None)
        return self._store.clear(watch_id)

    # --- create ------------------------------------------------------------

    def create(
        self,
        *,
        condition: str,
        verifier: dict,
        watch_id: str | None = None,
        interval_s: float | None = None,
        deadline: float | None = None,
        stall_after: int | None = None,
        run_prompt: str = "",
        run_session: str = "",
        trusted: bool = False,
    ) -> tuple[bool, str, Watch | None]:
        """Create a watch. ``trusted=False`` (agent/plugin/SDK) allows ONLY a ``plugin``
        verifier (like ``set_goal_safe``); ``trusted=True`` (operator ``/api/watches``,
        gated to operator-tier by the ADR 0066 ceiling) accepts any verifier type. A blank
        ``watch_id`` is derived from the condition (idempotent: same condition → same id,
        replaced). Returns ``(ok, message, watch|None)``."""
        if not condition:
            return (False, "a watch condition is required.", None)
        verifier = verifier or {"type": "llm"}
        vtype = verifier.get("type", "llm")
        if vtype not in VERIFIERS:
            return (False, f"unknown verifier type {vtype!r}; known: {', '.join(sorted(VERIFIERS))}.", None)
        if not trusted and vtype not in SAFE_PROGRAMMATIC_VERIFIERS:
            return (
                False,
                f"programmatic watches must use a 'plugin' verifier (got {vtype!r}); "
                "command/test/ci/data verifiers are operator-only — create them via POST /api/watches.",
                None,
            )
        if vtype == "plugin" and not verifier.get("check"):
            return (False, "a plugin verifier needs a 'check' (the <plugin-id>:<name>).", None)
        wid = (watch_id or "").strip() or self._derive_id(condition)
        watch = Watch(
            id=wid,
            condition=condition,
            verifier=verifier,
            interval_s=interval_s,
            deadline=deadline,
            stall_after=stall_after,
            run_prompt=run_prompt or "",
            run_session=run_session or "",
        )
        self._store.set(watch)
        return (True, f"Watch created. {watch.status_line()}", watch)

    @staticmethod
    def _derive_id(condition: str) -> str:
        """A stable id from the condition — slug + short hash, so re-creating the same
        condition replaces it (idempotent) while distinct conditions get distinct ids. Pass an
        explicit ``watch_id`` to hold two watches on the identical condition."""
        slug = "".join(c if c.isalnum() else "-" for c in (condition or "watch").lower()).strip("-")[:24].strip("-")
        h = hashlib.sha1((condition or "").encode()).hexdigest()[:6]
        return f"{slug or 'watch'}-{h}"

    @staticmethod
    def _parse_deadline(value) -> float | None:
        """A watch deadline: a number = epoch seconds, or an ISO-8601 string
        (``datetime.fromisoformat``) → epoch seconds. Unparseable → None (no deadline)."""
        if value is None or isinstance(value, bool):  # bool is an int subclass — reject it
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            from datetime import datetime

            try:
                return datetime.fromisoformat(value.strip()).timestamp()
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_stall_after(value) -> int | None:
        """A watch stall threshold: a positive int (checks) or None."""
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # --- evaluation --------------------------------------------------------

    async def evaluate(self, watch_id: str) -> str | None:
        """Run one watch's verifier out-of-band. Met → finish+react; deadline passed →
        ``expired``; ``stall_after`` unchanged checks → ``on_stalled`` (stays active). Returns
        the terminal status, or ``None`` while still active.

        Serialized per watch id (an ``asyncio.Lock``) so the cadence ``tick_all`` and an
        event-driven ``evaluate_now`` can't interleave a read-mutate-write on the SAME watch —
        which would drop a stall increment or re-activate a just-finished watch. Distinct watch
        ids never block each other."""
        async with self._lock_for(watch_id):
            return await self._evaluate_unlocked(watch_id)

    async def _evaluate_unlocked(self, watch_id: str) -> str | None:
        watch = self._store.get(watch_id)
        if watch is None or not watch.active:
            return None
        ctx = VerifyContext(
            config=self._config, condition=watch.condition, last_text="", tool_summary="", cwd=os.getcwd()
        )
        result = await run_verifier(watch.verifier, ctx)
        from time import time

        now = time()
        if result.met:
            return await self._finish(watch, "met", result.reason or "verifier passed", result.evidence)
        if watch.deadline is not None and now >= watch.deadline:
            return await self._finish(watch, "expired", "deadline passed before the watch met", result.evidence)

        unchanged = result.reason == watch.last_reason and result.evidence == watch.last_evidence
        watch.stall_streak = (watch.stall_streak + 1) if unchanged else 0
        if not unchanged:
            watch.stalled_notified = False
        if watch.stall_after and watch.stall_streak >= watch.stall_after and not watch.stalled_notified:
            watch.stalled_notified = True
            from graph.watches.hooks import fire_watch_hook

            await fire_watch_hook("on_stalled", watch)
            self._publish("watch.stalled", watch, result.reason)
        watch.last_reason = result.reason
        watch.last_evidence = result.evidence
        watch.last_checked = now
        self._store.set(watch)
        return None

    async def evaluate_now(self, watch_id: str) -> str | None:
        """Event-driven fast path — a plugin calls this from its own state-change path so a
        met watch is caught promptly instead of at the next tick. Same semantics as evaluate."""
        return await self.evaluate(watch_id)

    async def tick_all(self) -> int:
        """Evaluate every active watch out-of-band (verifier-only, no agent turn). The server
        runs this on a cadence. Returns how many reached a terminal state this tick."""
        finished = 0
        for watch in self.active_watches():
            try:
                status = await self.evaluate(watch.id)
            except Exception:  # noqa: BLE001 — one bad watch must not stop the tick
                log.exception("[watch] tick failed for %s", watch.id)
                continue
            if status is not None:
                finished += 1
        return finished

    async def _finish(self, watch: Watch, status: str, reason: str, evidence: str = "") -> str:
        from time import time

        from graph.watches.hooks import fire_watch_hook

        watch.status = status
        watch.last_reason = reason
        if evidence:
            watch.last_evidence = evidence
        watch.finished_at = time()
        self._store.set(watch)

        # Reaction (ADR 0067 D3): on MET, enqueue the follow-up prompt as a one-shot agent
        # turn in the watch's target session — non-blocking, reuses the tested run_in_session
        # primitive. Skipped when there's no prompt or no target session (hooks still fire).
        if status == "met" and (watch.run_prompt or "").strip() and (watch.run_session or "").strip():
            try:
                from graph.sdk import run_in_session

                run_in_session(watch.run_session, watch.run_prompt, job_id=f"watch-{watch.id}")
            except Exception:  # noqa: BLE001 — a reaction failure must not break the tick
                log.exception("[watch] run_in_session reaction failed for %s", watch.id)

        await fire_watch_hook("on_met" if status == "met" else "on_expired", watch)
        self._publish("watch.met" if status == "met" else "watch.expired", watch, reason)
        return status

    def _publish(self, topic: str, watch: Watch, reason: str = "") -> None:
        try:
            from graph.plugins.host import HOST

            if HOST.publish:
                HOST.publish(
                    topic,
                    {"id": watch.id, "condition": watch.condition, "status": watch.status, "reason": reason},
                )
        except Exception:  # noqa: BLE001
            pass
