"""Scheduler protocol — the contract the backend honors.

``LocalScheduler`` implements this shape; the agent-facing tools in
``tools/lg_tools.py`` only see the protocol, so it stays a clean seam for an
alternative backend a fork might add.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


@dataclass
class Job:
    """A scheduled future invocation.

    ``schedule`` is either a 5-field cron expression (e.g.
    ``"0 9 * * 1-5"``) or an ISO-8601 datetime for one-shot fires
    (e.g. ``"2026-05-01T15:00:00+00:00"``). Backends auto-detect.

    ``agent_name`` namespaces the job — a shared sqlite path can serve N
    protoAgent instances without cross-firing.
    """

    id: str
    prompt: str
    schedule: str
    agent_name: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    next_fire: str | None = None  # ISO; None means "compute on save"
    last_fire: str | None = None
    # IANA timezone the cron expression is evaluated in (e.g. "America/Chicago").
    # None = UTC. Ignored for one-shot ISO schedules (those carry their own offset).
    timezone: str | None = None
    # A2A contextId to fire the job INTO (None → the durable Activity thread, the
    # default for scheduled work). The `wait` tool sets this to the originating
    # chat session so a yield-and-resume continues that conversation's thread
    # rather than landing in Activity (ADR 0053 same-session resume).
    context_id: str | None = None
    enabled: bool = True
    # Auto-expiry (#2992): a recurring schedule shouldn't run forever by default.
    # ``ttl`` is a duration — human shorthand ("7d", "24h", "2w") or ISO-8601
    # ("P7D") — after which (measured from ``created_at``) the job auto-cancels.
    # ``max_fires`` auto-cancels after N firings; ``fire_count`` tracks them.
    ttl: str | None = None
    max_fires: int | None = None
    fire_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SchedulerBackend(Protocol):
    """The minimum surface a backend implements.

    Methods are sync because the agent tools wrap them in their own
    async functions; a backend that needs async I/O handles it internally.
    """

    name: str  # short label for logs / agent-facing strings: "local"

    def add_job(
        self,
        prompt: str,
        schedule: str,
        *,
        job_id: str | None = None,
        timezone: str | None = None,
        context_id: str | None = None,
        ttl: str | None = None,
        max_fires: int | None = None,
    ) -> Job:
        """Persist a new job. Returns the stored ``Job`` (with
        backend-assigned id and next_fire if the caller didn't set them).

        ``timezone`` is an IANA name the cron expression is evaluated in
        (None = UTC). Raises ``ValueError`` for malformed schedule/timezone.

        ``context_id`` is the A2A contextId to fire into (None → the durable
        Activity thread). Used for same-session resume (ADR 0053); backends that
        can't target a context (remote schedulers) may ignore it.

        ``ttl`` / ``max_fires`` auto-expire the job (#2992): after
        ``created_at + ttl`` elapses, or after ``max_fires`` firings, the
        backend cancels it. ``ttl`` must satisfy ``parse_ttl`` — raises
        ``ValueError`` for an unparseable duration or ``max_fires < 1``."""
        ...

    def cancel_job(self, job_id: str) -> bool:
        """Remove a job. Returns ``True`` if a row was deleted."""
        ...

    def update_job(
        self,
        job_id: str,
        prompt: str,
        schedule: str,
        *,
        timezone: str | None = None,
    ) -> Job:
        """Edit an existing job in place: replace its ``prompt`` / ``schedule`` /
        ``timezone`` and recompute the next fire. Returns the updated ``Job``.

        Atomic from the operator's view (vs cancel-then-re-add): the id, created_at
        and last_fire are preserved. Raises ``ValueError`` if no such job exists or
        the schedule/timezone is malformed."""
        ...

    def list_jobs(self) -> list[Job]:
        """All jobs visible to the calling agent. Implementations are
        responsible for filtering by ``agent_name`` so multi-agent
        deployments stay isolated."""
        ...

    async def start(self) -> None:
        """Start any background polling. No-op for a backend that doesn't
        need it."""
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


# ── job TTL (#2992) ─────────────────────────────────────────────────────────

_TTL_SHORTHAND = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)
_TTL_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_ISO_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+(?:\.\d+)?)W)?"
    r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def parse_ttl(ttl: str) -> timedelta:
    """Parse a job TTL string into a ``timedelta``.

    Accepts a human shorthand — ``"7d"``, ``"24h"``, ``"2w"``, ``"30m"``,
    ``"90s"`` — or an ISO-8601 duration — ``"P7D"``, ``"PT12H"``,
    ``"P1DT6H"``. Raises ``ValueError`` for anything else (including a
    zero/negative duration), so a nonsense TTL is rejected at ``add_job``
    time rather than silently never expiring.
    """
    text = (ttl or "").strip()
    if not text:
        raise ValueError("scheduler: ttl must be a non-empty duration string")
    m = _TTL_SHORTHAND.match(text)
    if m:
        amount, unit = m.groups()
        delta = timedelta(seconds=float(amount) * _TTL_UNIT_SECONDS[unit.lower()])
    else:
        m = _ISO_DURATION.match(text)
        if not m or not any(m.groupdict().values()):
            raise ValueError(
                f"scheduler: unparseable ttl {ttl!r} — use a shorthand like "
                f"'7d' / '24h' / '2w' or an ISO-8601 duration like 'P7D'"
            )
        parts = {k: float(v) for k, v in m.groupdict().items() if v}
        delta = timedelta(
            weeks=parts.get("weeks", 0.0),
            days=parts.get("days", 0.0),
            hours=parts.get("hours", 0.0),
            minutes=parts.get("minutes", 0.0),
            seconds=parts.get("seconds", 0.0),
        )
    if delta <= timedelta(0):
        raise ValueError(f"scheduler: ttl {ttl!r} must be a positive duration")
    return delta
