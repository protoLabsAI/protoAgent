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
        event_publish=None,
    ) -> None:
        self.agent_name = agent_name
        self._invoke_url = invoke_url.rstrip("/")
        self.store = store
        self._api_key = api_key or ""
        self._bearer = bearer_token or ""
        # (topic, data) -> None — the server's event bus, so a live console gets a
        # ``background.started`` push the moment a job is spawned (ADR 0050). Optional;
        # completion is published by the terminal hook (which already holds the bus).
        self._publish = event_publish
        if fire_timeout_s is not None:
            self._fire_timeout_s = fire_timeout_s
        else:
            try:
                self._fire_timeout_s = float(
                    os.environ.get("BACKGROUND_FIRE_TIMEOUT_S", _DEFAULT_FIRE_TIMEOUT_S)
                )
            except ValueError:
                self._fire_timeout_s = _DEFAULT_FIRE_TIMEOUT_S
        # Hold the detached fire tasks so they aren't GC'd mid-flight (the cause of
        # "Task was destroyed but it is pending"); discard on completion.
        self._fire_tasks: set[asyncio.Task] = set()

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
        t = asyncio.create_task(
            self._fire(job_id, fired_prompt), name=f"background.fire.{job_id}"
        )
        self._fire_tasks.add(t)
        t.add_done_callback(self._fire_tasks.discard)
        log.info("[background] spawned %s (%s): %s", job_id, subagent_type, description)
        # Live push so a still-open spawning chat shows the job starting (ADR 0050).
        if self._publish is not None:
            try:
                self._publish(
                    "background.started",
                    {
                        "job_id": job_id,
                        "status": "running",
                        "subagent_type": subagent_type,
                        "description": description,
                        "origin_session": origin_session or "",
                    },
                )
            except Exception:  # noqa: BLE001 — the event is best-effort
                log.exception("[background] started-event publish failed for %s", job_id)
        return job_id

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
            "jsonrpc": "2.0", "id": str(uuid.uuid4()),
            "method": "CancelTask", "params": {"id": task_id},
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
            job_id, "canceled", (self.store.get(job_id) or job).result or "Canceled.",
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
        try:
            async with httpx.AsyncClient(timeout=self._fire_timeout_s) as client:
                r = await client.post(f"{self._invoke_url}/a2a", headers=headers, json=body)
            if r.status_code >= 400:
                log.error(
                    "[background] fire failed for %s: HTTP %d %s",
                    job_id, r.status_code, r.text[:200],
                )
                self.store.mark_complete(
                    job_id, "failed", f"Background turn failed to start: HTTP {r.status_code}."
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("[background] fire exception for %s", job_id)
            self.store.mark_complete(
                job_id, "failed", f"Background turn delivery error: {exc}"
            )


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

    header = (
        f"[Background task — running detached as the '{subagent_type}' role]\n"
        f"Task: {description}\n"
    )
    guidance = (
        "\n\nWork autonomously to completion and end your turn with the finished result "
        "as your final message — it will be delivered back to the conversation that "
        "requested this task."
    )
    if role:
        return f"{header}\n{role}\n\n---\n\n{prompt}{guidance}"
    return f"{header}\n{prompt}{guidance}"
