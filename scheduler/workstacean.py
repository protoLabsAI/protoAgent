"""WorkstaceanScheduler — HTTP adapter to a protoWorkstacean install.

Activated automatically when ``WORKSTACEAN_API_BASE`` and
``WORKSTACEAN_API_KEY`` are set (see ``server.py``).

Speaks Workstacean's ``POST /publish`` API as documented at
https://protolabsai.github.io/protoWorkstacean/reference/scheduler/.
Every job is namespaced with the agent's name so multiple protoAgent
forks (e.g. ``gina-personal`` + ``gina-work``) can share one
Workstacean install without cross-firing:

- Job IDs are prefixed: ``{agent_name}-{user_id_or_uuid}``
- Topics are namespaced: ``cron.{agent_name}``

The adapter is fire-and-forget — Workstacean owns scheduling state.
``list_jobs()`` returns an empty list because Workstacean's list
action publishes asynchronously — strict local introspection requires
the local backend.

Note: Workstacean today does not natively dispatch to A2A endpoints;
forks need to wire their Workstacean install to route ``cron.*``
topics to the agent's A2A endpoint. See the linked guide for the
recommended bridge config.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from scheduler.interface import Job, parse_iso_to_utc, is_cron

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10


class WorkstaceanScheduler:
    """HTTP adapter to a Workstacean ``/publish`` endpoint."""

    name = "workstacean"

    def __init__(
        self,
        agent_name: str,
        *,
        base_url: str,
        api_key: str,
        topic_prefix: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        if not base_url:
            raise ValueError("WorkstaceanScheduler: base_url is required")
        if not api_key:
            raise ValueError("WorkstaceanScheduler: api_key is required")
        self.agent_name = agent_name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # Namespacing: topic_prefix governs which Workstacean topic the
        # job fires on. Default = ``cron.<agent>``. Forks can override
        # via ``WORKSTACEAN_TOPIC_PREFIX`` to integrate with existing
        # bus conventions.
        self._topic_prefix = topic_prefix or f"cron.{agent_name}"
        self._timeout_s = timeout_s

    # ── public API ──────────────────────────────────────────────────────────

    def add_job(self, prompt: str, schedule: str, *, job_id: str | None = None) -> Job:
        if not prompt or not prompt.strip():
            raise ValueError("scheduler: prompt is required")
        # Validate the schedule eagerly so a malformed expr fails at
        # tool-call time, not silently inside Workstacean.
        _validate_schedule(schedule)

        normalized_id = self._namespaced_id(job_id)
        topic = f"{self._topic_prefix}.{normalized_id}"
        # Workstacean expects an outer ``command.schedule`` topic and
        # the inner ``payload`` carries both the trigger schedule and
        # the actual message that will be fired. The inner ``topic``
        # is what Workstacean publishes to when the schedule fires —
        # so it has to be something a downstream A2A bridge subscribes
        # to. Default convention: ``cron.<agent>.<job-id>``.
        body = {
            "topic": "command.schedule",
            "payload": {
                "action": "add",
                "id": normalized_id,
                "schedule": schedule,
                "topic": topic,
                "payload": {
                    "content": prompt,
                    "sender": "scheduler",
                    "channel": "a2a",
                    # Cross-system breadcrumb so the bridge knows which
                    # protoAgent fork the message belongs to.
                    "agent_name": self.agent_name,
                    "scheduler_job_id": normalized_id,
                },
            },
        }
        self._publish(body)

        return Job(
            id=normalized_id,
            prompt=prompt,
            schedule=schedule,
            agent_name=self.agent_name,
            next_fire=None,  # Workstacean owns the schedule state
        )

    def cancel_job(self, job_id: str) -> bool:
        body = {
            "topic": "command.schedule",
            "payload": {"action": "remove", "id": self._namespaced_id(job_id)},
        }
        try:
            self._publish(body)
            return True
        except RuntimeError as exc:
            log.warning("[scheduler] workstacean cancel failed: %s", exc)
            return False

    def list_jobs(self) -> list[Job]:
        """Returns ``[]`` from the adapter.

        Workstacean's ``list`` action publishes its response on the
        ``schedule.list`` topic — there is no synchronous reply on
        ``/publish``. Subscribing to that topic from inside a
        protoAgent process (without a full bus client) is more
        machinery than this adapter is the right layer for. Forks
        that need live introspection should run the local backend or
        query Workstacean directly.
        """
        return []

    async def start(self) -> None:
        # Workstacean owns scheduling state — nothing to start here.
        log.info(
            "[scheduler] workstacean backend ready: agent=%s base=%s topic=%s.*",
            self.agent_name, self._base_url, self._topic_prefix,
        )

    async def stop(self) -> None:
        return None

    # ── helpers ─────────────────────────────────────────────────────────────

    def _publish(self, body: dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json", "X-API-Key": self._api_key}
        try:
            r = httpx.post(
                f"{self._base_url}/publish",
                headers=headers,
                json=body,
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"workstacean publish failed: {exc}") from exc
        if r.status_code >= 400:
            raise RuntimeError(
                f"workstacean publish HTTP {r.status_code}: {r.text[:200]}"
            )

    def _namespaced_id(self, job_id: str | None) -> str:
        suffix = job_id or uuid.uuid4().hex[:12]
        prefix = f"{self.agent_name}-"
        return suffix if suffix.startswith(prefix) else prefix + suffix


def _validate_schedule(schedule: str) -> None:
    """Validate cron expression OR ISO datetime. Raises ValueError."""
    if is_cron(schedule):
        from croniter import croniter
        try:
            croniter(schedule)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid cron expression {schedule!r}: {exc}") from exc
        return
    parse_iso_to_utc(schedule)  # raises ValueError on malformed ISO
