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
        # Hold the detached fire tasks so they aren't GC'd mid-flight (the cause of
        # "Task was destroyed but it is pending"); discard on completion.
        self._fire_tasks: set[asyncio.Task] = set()
        # job_id -> the running asyncio.Task for a ``spawn_work`` (deterministic) job, so
        # ``cancel`` can stop the coroutine directly (a work job has no A2A task handle).
        self._work_tasks: dict[str, asyncio.Task] = {}

    async def spawn(
        self,
        *,
        origin_session: str,
        subagent_type: str,
        description: str,
        prompt: str,
    ) -> str:
        """Register a job and fire it detached. Returns the opaque job id immediately."""
        job_id = self.store.create(
            agent_name=self.agent_name,
            origin_session=origin_session or "",
            subagent_type=subagent_type,
            description=description,
            prompt=prompt,
        )
        fired_prompt = _build_fired_prompt(subagent_type, description, prompt)
        t = asyncio.create_task(self._fire(job_id, fired_prompt), name=f"background.fire.{job_id}")
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

        headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        if self._api_key:
            headers["X-API-Key"] = self._api_key
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

    async def _fire(self, job_id: str, prompt: str) -> None:
        """POST the job to our own /a2a as a turn in a dedicated background context.

        On any delivery failure (non-2xx / network / timeout), mark the job failed —
        but only if the terminal hook hasn't already settled it (mark_complete is a
        no-op on an already-terminal row), so a slow-turn timeout can't clobber a
        result that actually landed.
        """
        import httpx

        # A2A 1.0 wire shape (matches scheduler/local.py:_fire): SendMessage, ROLE_USER,
        # {text} parts, contextId + metadata on the message, A2A-Version header.
        headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        message_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": prompt}],
                    "messageId": message_id,
                    # Dedicated, isolated context per job — keeps the background turn's
                    # history out of the originating chat thread; the job id rides in
                    # the context so the terminal hook can map back without metadata.
                    "contextId": f"background:{job_id}",
                    "metadata": {
                        "origin": "background",
                        "trigger": job_id,
                        "background_job_id": job_id,
                    },
                },
            },
        }
        # Gate the actual self-POST on the concurrency semaphore so a fan-out can't open more
        # than ``_max_concurrency`` full turns at once. The slot is held for the WHOLE turn —
        # the A2A handler runs the turn synchronously before the POST returns — which is
        # exactly the bound we want (concurrent running turns, not just in-flight requests).
        async with self._sem:
            try:
                async with httpx.AsyncClient(timeout=self._fire_timeout_s) as client:
                    r = await client.post(f"{self._invoke_url}/a2a", headers=headers, json=body)
                if r.status_code >= 400:
                    log.error(
                        "[background] fire failed for %s: HTTP %d %s",
                        job_id,
                        r.status_code,
                        r.text[:200],
                    )
                    self.store.mark_complete(job_id, "failed", f"Background turn failed to start: HTTP {r.status_code}.")
            except Exception as exc:  # noqa: BLE001
                log.exception("[background] fire exception for %s", job_id)
                self.store.mark_complete(job_id, "failed", f"Background turn delivery error: {exc}")


def _build_fired_prompt(subagent_type: str, description: str, prompt: str) -> str:
    """Compose the message the background turn runs.

    The background turn runs the full lead graph (ADR 0050 — self-POST substrate), so the
    subagent's own system prompt is prepended as role guidance rather than enforced as a
    tool fence (per-subagent tool scoping for background jobs is deferred)."""
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
