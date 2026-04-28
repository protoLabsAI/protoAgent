"""Scheduler protocol — the contract every backend honors.

Both ``LocalScheduler`` and ``WorkstaceanScheduler`` implement this
shape. The agent-facing tools in ``tools/lg_tools.py`` only see the
protocol; swapping backends is a server.py-level decision.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass
class Job:
    """A scheduled future invocation.

    ``schedule`` is either a 5-field cron expression (e.g.
    ``"0 9 * * 1-5"``) or an ISO-8601 datetime for one-shot fires
    (e.g. ``"2026-05-01T15:00:00+00:00"``). Backends auto-detect.

    ``agent_name`` namespaces the job — one Workstacean install or
    shared sqlite path can serve N protoAgent instances without
    cross-firing.
    """

    id: str
    prompt: str
    schedule: str
    agent_name: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    next_fire: str | None = None        # ISO; None means "compute on save"
    last_fire: str | None = None
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SchedulerBackend(Protocol):
    """The minimum surface every backend implements.

    Methods are sync because the agent tools wrap them in their own
    async functions; backends that need to do async I/O (httpx in
    Workstacean's case) handle it internally.
    """

    name: str  # short label for logs / agent-facing strings: "local", "workstacean"

    def add_job(self, prompt: str, schedule: str, *, job_id: str | None = None) -> Job:
        """Persist a new job. Returns the stored ``Job`` (with
        backend-assigned id and next_fire if the caller didn't set them).

        Raises ``ValueError`` for malformed schedule strings."""
        ...

    def cancel_job(self, job_id: str) -> bool:
        """Remove a job. Returns ``True`` if a row was deleted."""
        ...

    def list_jobs(self) -> list[Job]:
        """All jobs visible to the calling agent. Implementations are
        responsible for filtering by ``agent_name`` so multi-agent
        deployments stay isolated."""
        ...

    async def start(self) -> None:
        """Start any background polling. No-op for backends that don't
        need it (Workstacean dispatches and forgets)."""
        ...

    async def stop(self) -> None:
        """Cleanly shut down background work."""
        ...


# ── shared helpers ──────────────────────────────────────────────────────────


_CRON_PATTERN = re.compile(r"^\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*$")


def is_cron(schedule: str) -> bool:
    """Heuristic: does ``schedule`` look like a 5-field cron expression?

    Used by both backends to decide between cron-iter and
    ``datetime.fromisoformat``. Doesn't validate semantics — that
    happens when the schedule is parsed.
    """
    return bool(_CRON_PATTERN.match(schedule)) and not _looks_like_iso(schedule)


def _looks_like_iso(schedule: str) -> bool:
    # ISO datetimes contain ``-`` and either ``T`` or a space between
    # date and time. Cron has neither in the first field.
    return "T" in schedule or _has_iso_date_prefix(schedule)


def _has_iso_date_prefix(schedule: str) -> bool:
    head = schedule.strip().split(" ", 1)[0]
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}", head))


def parse_iso_to_utc(schedule: str) -> datetime:
    """Parse an ISO-8601 datetime, treating naive inputs as UTC.

    Raises ``ValueError`` for malformed strings.
    """
    dt = datetime.fromisoformat(schedule)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
