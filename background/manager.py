"""BackgroundManager — spawns background subagent jobs as detached A2A turns (ADR 0050).

A background job runs as a **self-POSTed A2A turn**: the manager POSTs a ``SendMessage``
to the agent's own ``/a2a`` endpoint in a dedicated ``background:<job_id>`` context,
detached from the foreground turn that requested it. This reuses the scheduler's proven
self-invoke pattern (``scheduler/local.py``), so a background job inherits the durable A2A
task store, lifecycle states, telemetry, and audit for free — and the terminal hook
(``server/a2a.py``) marks the job complete + drains the result back to its originating
chat session.

The fire is awaited inside an ``asyncio.create_task`` so the requesting tool returns
immediately; the connection is held open for the whole turn (the A2A handler runs the turn
synchronously), then the row is already terminal via the terminal hook. A delivery failure
marks the row ``failed`` so it can never stick on ``running``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from background.store import BackgroundStore

log = logging.getLogger(__name__)

_DEFAULT_FIRE_TIMEOUT_S = 1800.0  # background turns can run long (research + tools); generous
_DEFAULT_MAX_CONCURRENCY = 3  # cap on concurrent background turns so a fan-out can't swamp the gateway
# Straggler safety valve (#1766): if a fan-out batch hasn't fully settled within this many
# seconds (a member hung), force a PARTIAL push-resume so the finished reports still land.
_DEFAULT_BATCH_JOIN_TIMEOUT_S = 900.0
_TERMINAL_STATUSES = ("completed", "failed", "canceled")


class BackgroundManager:
    """Fires background subagent jobs and tracks them in a durable store."""

    def __init__(
        self,
        *,
        agent_name: str,
        invoke_url: str,
        store: BackgroundStore,
        api_key: str | None = None,
        bearer_token: str | None = None,
        fire_timeout_s: float | None = None,
        max_concurrency: int | None = None,
        batch_join_timeout_s: float | None = None,
        event_publish=None,
        on_terminal=None,
    ) -> None:
        self.agent_name = agent_name
        self._invoke_url = invoke_url.rstrip("/")
        self.store = store
        self._api_key = api_key or ""
        self._bearer = bearer_token or ""
        # (topic, data) -> None — the server's event bus, so a live console gets a
        # ``background.started`` push the moment a job is spawned (ADR 0050). Optional;
        # a subagent-turn job's completion is published by the A2A terminal hook (which
        # already holds the bus); a deterministic ``spawn_work`` job publishes its OWN
        # completion here (no A2A turn fires, so the terminal hook never runs for it).
        self._publish = event_publish
        # on_terminal(job) -> None — invoked when a ``spawn_work`` job settles, so the
        # server can run the same idle-wake the A2A terminal hook does (ADR 0050 Phase 2)
        # without this package importing ``server`` (injected to respect the layering).
        self._on_terminal = on_terminal
        if fire_timeout_s is not None:
            self._fire_timeout_s = fire_timeout_s
        else:
            try:
                self._fire_timeout_s = float(os.environ.get("BACKGROUND_FIRE_TIMEOUT_S", _DEFAULT_FIRE_TIMEOUT_S))
            except ValueError:
                self._fire_timeout_s = _DEFAULT_FIRE_TIMEOUT_S
        # Bound how many background turns run at once. A wide fan-out — ``task_batch`` with
        # run_in_background, or several ``task(run_in_background=True)`` calls — would
        # otherwise open one full lead-graph turn per job against the gateway at the same
        # time. The cap gates the actual self-POST in ``_fire`` (which holds its slot for the
        # whole turn); jobs past the cap queue at the semaphore. A queued job's store row
        # still reads ``running`` (it IS accepted) — cancel/reconcile both handle a row whose
        # turn hasn't fired yet. Override with ``BACKGROUND_MAX_CONCURRENCY``.
        if max_concurrency is not None:
            self._max_concurrency = max(1, int(max_concurrency))
        else:
            try:
                self._max_concurrency = max(1, int(os.environ.get("BACKGROUND_MAX_CONCURRENCY", _DEFAULT_MAX_CONCURRENCY)))
            except ValueError:
                self._max_concurrency = _DEFAULT_MAX_CONCURRENCY
        self._sem = asyncio.Semaphore(self._max_concurrency)
        # Straggler-join timeout (#1766). Env ``BACKGROUND_BATCH_JOIN_TIMEOUT_S``.
        if batch_join_timeout_s is not None:
            self._batch_join_timeout_s = batch_join_timeout_s
        else:
            try:
                self._batch_join_timeout_s = float(
                    os.environ.get("BACKGROUND_BATCH_JOIN_TIMEOUT_S", _DEFAULT_BATCH_JOIN_TIMEOUT_S)
                )
            except ValueError:
                self._batch_join_timeout_s = _DEFAULT_BATCH_JOIN_TIMEOUT_S
        # Hold the detached fire tasks so they aren't GC'd mid-flight (the cause of
        # "Task was destroyed but it is pending"); discard on completion.
        self._fire_tasks: set[asyncio.Task] = set()
        # job_id -> the running asyncio.Task for a ``spawn_work`` (deterministic) job, so
        # ``cancel`` can stop the coroutine directly (a work job has no A2A task handle).
        self._work_tasks: dict[str, asyncio.Task] = {}
        # Fan-out batch-join (#1766). A batch (all jobs sharing a spawning turn id)
        # push-resumes ONCE, when its last member settles. ``_joined_batches`` is the
        # single-fire guard — a SYNCHRONOUS check-and-set (``_claim_batch``) so two
        # concurrent settles, or a settle racing the straggler timeout, can't both fire.
        # ``_batch_timeouts`` holds the per-batch straggler-timeout task (a hung member
        # can't strand the finished reports forever); also kept in ``_fire_tasks`` so it
        # isn't GC'd mid-sleep.
        self._joined_batches: set[str] = set()
        self._batch_timeouts: dict[str, asyncio.Task] = {}

    async def spawn(
        self,
        *,
        origin_session: str,
        subagent_type: str,
        description: str,
        prompt: str,
        origin_incognito: bool = False,
        batch_id: str | None = None,
    ) -> str:
        """Register a job and fire it detached. Returns the opaque job id immediately.

        ``origin_incognito`` records that the spawning thread was incognito (ADR 0069
        D3b → ADR 0070): the completion then skips the push-resume nudge and the
        knowledge-store indexing — no memory trail — while the report still lives in
        the jobs DB and drains into the origin session normally.

        ``batch_id`` (#1766) tags the job as a member of a fan-out spawned by one turn
        (task_batch's specs, or several ``task(run_in_background=True)`` in a turn — all
        stamp the emitting turn's id), so the completions coalesce into ONE push-resume
        when the last member settles. ``None`` for a lone spawn (a singleton)."""
        job_id = self.store.create(
            agent_name=self.agent_name,
            origin_session=origin_session or "",
            subagent_type=subagent_type,
            description=description,
            prompt=prompt,
            origin_incognito=origin_incognito,
            batch_id=batch_id,
        )
        fired_prompt = _build_fired_prompt(subagent_type, description, prompt)
        fence = _subagent_fence(subagent_type)
        t = asyncio.create_task(self._fire(job_id, fired_prompt, fence), name=f"background.fire.{job_id}")
        self._fire_tasks.add(t)
        t.add_done_callback(self._fire_tasks.discard)
        log.info("[background] spawned %s (%s): %s", job_id, subagent_type, description)
        # Live push so a still-open spawning chat shows the job starting (ADR 0050).
        self._publish_started(job_id, subagent_type, description, origin_session or "")
        return job_id

    # ── deterministic work jobs (ADR 0050 — non-subagent background) ──────────

    async def spawn_work(
        self,
        *,
        origin_session: str,
        kind: str,
        description: str,
        work,
        detail: str = "",
        origin_incognito: bool = False,
        batch_id: str | None = None,
    ) -> str:
        """Register and run a deterministic background job — a plain coroutine, NOT an
        LLM subagent turn — through the same durable store + concurrency cap + event
        stream + drain-on-next-turn notification as ``spawn`` (ADR 0050).

        ``work`` is a zero-arg async callable that does the work and returns the result
        text to deliver back to the originating session. ``kind`` is a short label
        (stored as ``subagent_type``, e.g. ``"ingest"``); ``detail`` is recorded as the
        job's ``prompt`` (e.g. the source). Returns the opaque job id immediately. Use
        this for long deterministic operations (media transcription/ingest) that must
        not block the foreground turn."""
        job_id = self.store.create(
            agent_name=self.agent_name,
            origin_session=origin_session or "",
            subagent_type=kind,
            description=description,
            prompt=detail,
            origin_incognito=origin_incognito,
            batch_id=batch_id,
        )
        t = asyncio.create_task(
            self._run_work(job_id, kind, description, origin_session or "", work),
            name=f"background.work.{job_id}",
        )
        self._work_tasks[job_id] = t
        self._fire_tasks.add(t)
        t.add_done_callback(self._fire_tasks.discard)
        t.add_done_callback(lambda _t, jid=job_id: self._work_tasks.pop(jid, None))
        log.info("[background] spawned work %s (%s): %s", job_id, kind, description)
        self._publish_started(job_id, kind, description, origin_session or "")
        return job_id

    async def _run_work(self, job_id: str, kind: str, description: str, origin_session: str, work) -> None:
        """Run a ``spawn_work`` coroutine under the shared concurrency cap, settle the
        store row, publish ``background.completed``, and fire the optional terminal hook
        (idle-wake). Mirrors what the A2A terminal hook does for subagent-turn jobs."""
        async with self._sem:  # same cap as background turns — one fan-out can't swamp the gateway
            try:
                result = await work()
                status, text = "completed", (str(result) if result is not None else "")
            except asyncio.CancelledError:
                # cancel() already settled the row + published; just unwind.
                raise
            except Exception as exc:  # noqa: BLE001 — any failure settles the row, never hangs on running
                log.exception("[background] work job %s failed", job_id)
                status, text = "failed", (str(exc) or "Background work failed.")
        if not self.store.mark_complete(job_id, status, text):
            return  # already terminal (e.g. canceled mid-flight) — don't double-announce
        self._publish_completed(job_id, status, kind, description, origin_session, text)
        if self._on_terminal is not None:
            try:
                job = self.store.get(job_id)
                if job is not None:
                    self._on_terminal(job)
            except Exception:  # noqa: BLE001 — the wake is best-effort
                log.exception("[background] on_terminal hook failed for %s", job_id)

    # ── event-bus helpers (shared by spawn + spawn_work) ──────────────────────

    def _publish_started(self, job_id: str, kind: str, description: str, origin_session: str) -> None:
        if self._publish is None:
            return
        try:
            self._publish(
                "background.started",
                {
                    "job_id": job_id,
                    "status": "running",
                    "subagent_type": kind,
                    "description": description,
                    "origin_session": origin_session,
                },
            )
        except Exception:  # noqa: BLE001 — the event is best-effort
            log.exception("[background] started-event publish failed for %s", job_id)

    def _publish_turn(
        self, topic: str, *, session_id: str, origin: str, trigger: str, ok: bool | None = None
    ) -> None:
        """Emit a turn-lifecycle event (#1767) around a server-initiated self-POST so an
        open console can render its typing indicator during an otherwise-invisible turn
        (the push-resume nudge holds the connection open for the WHOLE origin-session
        turn). Best-effort — a publish failure never disturbs the fire."""
        if self._publish is None:
            return
        data = {"session_id": session_id, "origin": origin, "trigger": trigger}
        if ok is not None:
            data["ok"] = ok
        try:
            self._publish(topic, data)
        except Exception:  # noqa: BLE001 — the event is best-effort
            log.exception("[background] %s publish failed for %s", topic, trigger)

    def _publish_completed(
        self, job_id: str, status: str, kind: str, description: str, origin_session: str, text: str
    ) -> None:
        """Match the A2A terminal hook's ``background.completed`` payload (server/a2a.py)
        so the console renders a work job's card identically to a subagent's."""
        if self._publish is None:
            return
        preview = text if len(text) <= 2000 else text[:2000] + "\n\n…_[truncated]_"
        try:
            self._publish(
                "background.completed",
                {
                    "job_id": job_id,
                    "status": status,
                    "subagent_type": kind,
                    "description": description,
                    "origin_session": origin_session,
                    "result": preview,
                },
            )
        except Exception:  # noqa: BLE001 — the event is best-effort
            log.exception("[background] completed-event publish failed for %s", job_id)

    async def cancel(self, job_id: str) -> dict:
        """Stop a running background job by cancelling its detached A2A turn (ADR 0051).

        Sends a real ``CancelTask`` for the job's recorded A2A task id — the SDK cancels
        the producer coroutine, which fires the executor's cancel telemetry and settles the
        row. Returns ``{ok, status, detail}``. Idempotent: a no-op on an already-terminal
        job."""
        job = self.store.get(job_id)
        if job is None:
            return {"ok": False, "status": "unknown", "detail": f"No background job {job_id}."}
        if job.status != "running":
            return {"ok": False, "status": job.status, "detail": f"Job {job_id} already {job.status}."}
        # A deterministic ``spawn_work`` job runs as a local coroutine, not an A2A turn —
        # cancel the task and settle the row directly (no CancelTask round-trip).
        work_task = self._work_tasks.get(job_id)
        if work_task is not None:
            work_task.cancel()
            self.store.mark_complete(job_id, "canceled", "Canceled.")
            self._publish_completed(job_id, "canceled", job.subagent_type, job.description, job.origin_session, "Canceled.")
            return {"ok": True, "status": "canceled", "detail": f"Canceled {job_id}."}
        task_id = job.a2a_task_id
        if not task_id:
            # The turn hasn't announced its task id yet — settle the row so it can't hang;
            # the turn may still finish server-side (mark_complete is then a no-op).
            self.store.mark_complete(job_id, "canceled", "Canceled before the turn registered a task id.")
            return {"ok": True, "status": "canceled", "detail": "Canceled (no task handle yet)."}

        import httpx

        headers = self._a2a_headers()
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "CancelTask",
            "params": {"id": task_id},
        }
        ok = True
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{self._invoke_url}/a2a", headers=headers, json=body)
            if r.status_code >= 400:
                log.warning("[background] CancelTask HTTP %d for %s", r.status_code, job_id)
                ok = False
        except Exception:  # noqa: BLE001
            log.exception("[background] CancelTask failed for %s", job_id)
            ok = False
        # The executor's cancel telemetry usually settles the row first (with partial
        # text); ensure it's settled regardless. mark_complete is idempotent.
        self.store.mark_complete(
            job_id,
            "canceled",
            (self.store.get(job_id) or job).result or "Canceled.",
        )
        return {"ok": ok, "status": "canceled", "detail": f"Canceled {job_id}."}

    # ── self-POST mechanics (shared by _fire and resume_origin — ADR 0050/0070) ─

    def _a2a_headers(self) -> dict:
        headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _send_a2a_message(self, *, context_id: str, text: str, metadata: dict) -> None:
        """POST one ``SendMessage`` turn to our own ``/a2a`` and hold the connection
        open until the turn finishes (the A2A handler runs it synchronously).

        A2A 1.0 wire shape (matches scheduler/local.py:_fire): SendMessage, ROLE_USER,
        ``{text}`` parts, contextId + metadata on the message, A2A-Version header.
        Raises on a non-2xx response or any network/timeout error — callers decide
        what a delivery failure means (a job fire marks the row failed; a push-resume
        nudge just logs and lets the drain deliver on the next manual turn)."""
        import httpx

        message_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": text}],
                    "messageId": message_id,
                    "contextId": context_id,
                    "metadata": metadata,
                },
            },
        }
        async with httpx.AsyncClient(timeout=self._fire_timeout_s) as client:
            r = await client.post(f"{self._invoke_url}/a2a", headers=self._a2a_headers(), json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

    async def _fire(self, job_id: str, prompt: str, fence: list[str] | None = None) -> None:
        """POST the job to our own /a2a as a turn in a dedicated background context.

        On any delivery failure (non-2xx / network / timeout), mark the job failed —
        but only if the terminal hook hasn't already settled it (mark_complete is a
        no-op on an already-terminal row), so a slow-turn timeout can't clobber a
        result that actually landed.
        """
        # Gate the actual self-POST on the concurrency semaphore so a fan-out can't open more
        # than ``_max_concurrency`` full turns at once. The slot is held for the WHOLE turn —
        # the A2A handler runs the turn synchronously before the POST returns — which is
        # exactly the bound we want (concurrent running turns, not just in-flight requests).
        async with self._sem:
            try:
                await self._send_a2a_message(
                    # Dedicated, isolated context per job — keeps the background turn's
                    # history out of the originating chat thread; the job id rides in
                    # the context so the terminal hook can map back without metadata.
                    context_id=f"background:{job_id}",
                    text=prompt,
                    metadata={
                        "origin": "background",
                        "trigger": job_id,
                        "background_job_id": job_id,
                        # Per-subagent tool fence (#1639): the chat entry stamps this on
                        # the turn's state and SubagentFenceMiddleware enforces it — the
                        # same allowlist the in-graph task path applies, now on detached
                        # runs too. Absent for non-registry types (no fence).
                        **({"subagent_fence": fence} if fence else {}),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("[background] fire failed for %s", job_id)
                self.store.mark_complete(job_id, "failed", f"Background turn delivery error: {exc}")

    async def resume_origin(self, job) -> bool:
        """Push-resume (ADR 0070 D1): submit a terse self-A2A nudge INTO the job's
        origin session, so the origin agent runs a turn NOW — the notified-gated
        drain (``server/chat.py``) attaches the actual ``<task-notification>`` to
        that turn, and the agent briefs the operator against the new data.

        Deliberately NOT gated on the concurrency semaphore: this is an
        origin-session turn, not a background job — queuing the briefing behind
        the very jobs it reports on would deadlock a full fan-out. A mid-turn
        origin session is safe: the A2A server serializes turns per thread_id
        (``server/chat.py:_thread_lock``), so the nudge queues and runs after the
        in-flight turn. Never raises; returns whether the nudge was delivered.
        On failure nothing is lost — ``notified`` is untouched, so the report
        still drains on the session's next manual turn."""
        verb = "failed" if job.status == "failed" else "finished"
        text = (
            f"[background job {job.id} ({job.description}) {verb} — its report notification "
            "is attached to this turn; review it and brief the operator]"
        )
        # Turn-lifecycle events (#1767): the nudge holds the connection open for the whole
        # origin-session turn (the agent briefs the operator against the drained report),
        # which the console can't otherwise see — no typing indicator, no stream. Emit
        # `turn.started` before and `turn.finished` after so an open origin-session tab
        # renders its typing indicator ("responding to background reports…") for the turn.
        session_id = job.origin_session
        self._publish_turn("turn.started", session_id=session_id, origin="background-resume", trigger=job.id)
        ok = False
        try:
            await self._send_a2a_message(
                context_id=session_id,
                text=text,
                metadata={
                    # NOT "background": the terminal hook routes origin=="background"
                    # turns back into _handle_background_terminal — the nudge turn is
                    # an ordinary origin-session turn with its own provenance.
                    "origin": "background-resume",
                    "trigger": job.id,
                    "background_job_id": job.id,
                },
            )
            ok = True
            return True
        except Exception as exc:  # noqa: BLE001 — push-resume is best-effort by contract
            log.warning(
                "[background] push-resume for %s into %s failed (%s) — the report will "
                "drain on that session's next turn",
                job.id,
                job.origin_session,
                exc,
            )
            return False
        finally:
            self._publish_turn(
                "turn.finished", session_id=session_id, origin="background-resume", trigger=job.id, ok=ok
            )

    # ── fan-out batch-join (#1766) ────────────────────────────────────────────

    def _claim_batch(self, batch_id: str) -> bool:
        """Atomically claim a batch for its single join nudge. Returns ``True`` if THIS
        caller won the claim (and must fire the join), ``False`` if the batch was already
        joined. SYNCHRONOUS — no ``await`` between the membership check and the add — so two
        concurrent settles (or a settle racing the straggler timeout) can't both win under
        asyncio's single-threaded scheduling. This is the no-double-fire guarantee."""
        if batch_id in self._joined_batches:
            return False
        self._joined_batches.add(batch_id)
        return True

    async def resume_for_terminal(self, job) -> bool | None:
        """Batch-aware terminal delivery (#1766). The server calls this in place of
        ``resume_origin`` when a background job settles, so a fan-out coalesces into ONE
        push-resume instead of N drip-fed briefings.

        - **Singleton** (no ``batch_id``, or a batch of one) → delegate to
          ``resume_origin`` — the UNCHANGED single-job push-resume. Returns its bool.
        - **Not the last member** to settle → hold: arm the straggler timeout once and
          return ``None`` (nothing delivered, NOT a failure — the last member delivers).
        - **Last member** to settle → win the single-fire claim and push-resume the WHOLE
          fan-out once via ``resume_origin_batch`` (the per-session drain then attaches
          every sibling report to that one turn). A duplicate last-member — the claim was
          already taken by a sibling or the straggler timeout — returns ``None``.

        Never raises. Incognito batches never reach here: the server's ``_should_auto_resume``
        guard skips an incognito job before calling this (all members of one turn share the
        incognito flag), so no push-resume fires — their reports still drain normally."""
        batch_id = getattr(job, "batch_id", None)
        if not batch_id or self.store.batch_size(batch_id) <= 1:
            return await self.resume_origin(job)
        if self.store.batch_outstanding(batch_id) > 0:
            # Siblings still running — hold this completion; the last to settle fires the
            # single coalesced nudge. Arm the straggler timeout so a hung member can't
            # strand the finished reports forever.
            self._ensure_batch_timeout(batch_id, job.origin_session)
            return None
        # Last member settled. Win the single-fire claim (or bail if a sibling / the
        # timeout already fired), then push-resume the whole fan-out exactly once.
        if not self._claim_batch(batch_id):
            return None
        self._cancel_batch_timeout(batch_id)  # settled naturally — no straggler nudge needed
        return await self.resume_origin_batch(batch_id, job.origin_session)

    def _ensure_batch_timeout(self, batch_id: str, origin_session: str) -> None:
        """Arm ONE straggler-timeout task for a batch, idempotently. If the batch hasn't
        joined within ``_batch_join_timeout_s`` (a member hung), force the join so the
        already-finished reports still land. Best-effort; no-op off-loop or if already
        armed/joined."""
        if batch_id in self._batch_timeouts or batch_id in self._joined_batches:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _straggle() -> None:
            try:
                await asyncio.sleep(self._batch_join_timeout_s)
                if not self._claim_batch(batch_id):
                    return  # the batch joined normally while we slept
                await self.resume_origin_batch(batch_id, origin_session)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the straggler join is best-effort
                log.exception("[background] batch straggler-join failed for %s", batch_id)

        t = loop.create_task(_straggle(), name=f"background.batch-timeout.{batch_id}")
        self._batch_timeouts[batch_id] = t
        self._fire_tasks.add(t)  # hold a ref so the sleep isn't GC'd mid-flight
        t.add_done_callback(self._fire_tasks.discard)
        t.add_done_callback(lambda _t, b=batch_id: self._batch_timeouts.pop(b, None))

    def _cancel_batch_timeout(self, batch_id: str) -> None:
        """Cancel a batch's straggler timeout once the batch joined normally (no straggler
        nudge needed). No-op when none is armed."""
        t = self._batch_timeouts.pop(batch_id, None)
        if t is not None and not t.done():
            t.cancel()

    async def resume_origin_batch(self, batch_id: str, origin_session: str) -> bool:
        """Push-resume a whole fan-out ONCE (#1766). Same self-A2A mechanics as
        ``resume_origin`` (a nudge into ``origin_session`` wrapped in ``turn.started`` /
        ``turn.finished``), but the text summarizes the batch — the per-session drain
        (``server/chat.py``) attaches EVERY sibling report to this one turn, so the agent
        synthesizes a single briefing across all of them. If a straggler forced an early
        join (members still running), the text says only the settled ones are attached and
        the rest will notify separately. Never raises; returns whether the nudge landed —
        on failure ``notified`` is untouched, so the reports still drain next manual turn."""
        counts = self.store.batch_status_counts(batch_id)
        total = self.store.batch_size(batch_id)
        running = counts.get("running", 0)
        settled_counts = {k: v for k, v in counts.items() if k in _TERMINAL_STATUSES}
        settled = sum(settled_counts.values())
        summary = ", ".join(f"{k} {v}" for k, v in sorted(settled_counts.items())) or "no reports"
        if running > 0:
            text = (
                f"[{settled} of {total} background jobs from your fan-out have finished ({summary}); "
                "their report notifications are attached to this turn — synthesize ONE briefing against "
                f"them. The remaining {running} are still running and will notify separately.]"
            )
        else:
            text = (
                f"[all {total} background jobs from your fan-out have finished ({summary}) — their report "
                "notifications are attached to this turn; synthesize ONE briefing against all of them]"
            )
        # Turn-lifecycle events (#1767), keyed on the batch id: the nudge holds the
        # connection open for the whole origin-session briefing turn.
        self._publish_turn("turn.started", session_id=origin_session, origin="background-resume", trigger=batch_id)
        ok = False
        try:
            await self._send_a2a_message(
                context_id=origin_session,
                text=text,
                metadata={
                    "origin": "background-resume",
                    "trigger": batch_id,
                    "background_batch_id": batch_id,
                },
            )
            ok = True
            return True
        except Exception as exc:  # noqa: BLE001 — push-resume is best-effort by contract
            log.warning(
                "[background] batch push-resume for %s into %s failed (%s) — the reports will "
                "drain on that session's next turn",
                batch_id,
                origin_session,
                exc,
            )
            return False
        finally:
            self._publish_turn(
                "turn.finished", session_id=origin_session, origin="background-resume", trigger=batch_id, ok=ok
            )


def _subagent_fence(subagent_type: str) -> list[str]:
    """The subagent's resolved tool allowlist for a detached run (#1639): the registry
    ``tools`` with any config override applied — the SAME fence the in-graph ``task``
    path enforces (mirrors ``operator_api.subagents.list_subagents``'s resolution).
    Empty when the type isn't a registry subagent (no fence). Best-effort: a
    resolution failure means no fence, never a failed fire."""
    try:
        from graph.subagents.config import SUBAGENT_REGISTRY

        registry_def = SUBAGENT_REGISTRY.get(subagent_type)
        if registry_def is None:
            return []
        tools = list(registry_def.tools or [])
        try:
            from runtime.state import STATE

            override = getattr(STATE.graph_config, subagent_type, None) if STATE.graph_config else None
            tools = list(getattr(override, "tools", None) or tools)
        except Exception:  # noqa: BLE001 — config overlay is best-effort
            pass
        return tools
    except Exception:  # noqa: BLE001
        return []


def _build_fired_prompt(subagent_type: str, description: str, prompt: str) -> str:
    """Compose the message the background turn runs.

    The background turn runs the full lead graph (ADR 0050 — self-POST substrate); the
    subagent's own system prompt is prepended as role guidance, and the tool allowlist
    is enforced separately via the ``subagent_fence`` fire metadata (#1639)."""
    role = ""
    try:
        from graph.prompts import build_subagent_prompt
        from graph.subagents.config import SUBAGENT_REGISTRY

        if subagent_type in SUBAGENT_REGISTRY:
            role = (build_subagent_prompt(subagent_type) or "").strip()
    except Exception:  # noqa: BLE001 — role guidance is best-effort
        role = ""

    header = f"[Background task — running detached as the '{subagent_type}' role]\nTask: {description}\n"
    guidance = (
        "\n\nWork autonomously to completion and end your turn with the finished result "
        "as your final message — it will be delivered back to the conversation that "
        "requested this task."
    )
    if role:
        return f"{header}\n{role}\n\n---\n\n{prompt}{guidance}"
    return f"{header}\n{prompt}{guidance}"
