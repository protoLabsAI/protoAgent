"""Google Calendar operations for the Google MCP server (Slice 2).

Pure functions over a ``googleapiclient`` Calendar ``service`` resource (injected
→ unit-testable with a mock). Read-only: today's agenda + free/busy.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any


def _day_bounds(now: datetime) -> tuple[str, str]:
    start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def todays_events(service: Any, now: datetime, calendar_id: str = "primary") -> list[dict]:
    """Events between local midnight and the next, ordered by start time.
    ``now`` carries the operator's tz (caller supplies it from identity)."""
    time_min, time_max = _day_bounds(now)
    resp = (
        service.events()
        .list(calendarId=calendar_id, timeMin=time_min, timeMax=time_max, singleEvents=True, orderBy="startTime")
        .execute()
    )
    out: list[dict] = []
    for ev in resp.get("items", []) or []:
        start = ev.get("start", {})
        out.append(
            {
                "summary": ev.get("summary", "(no title)"),
                # all-day events carry "date"; timed events carry "dateTime"
                "start": start.get("dateTime") or start.get("date") or "",
                "all_day": "date" in start and "dateTime" not in start,
                "location": ev.get("location", ""),
                "attendees": [a.get("email", "") for a in ev.get("attendees", []) or []],
            }
        )
    return out


def free_busy(service: Any, now: datetime, hours: int = 24, calendar_id: str = "primary") -> list[dict]:
    """Busy intervals over the next ``hours`` for ``calendar_id``."""
    time_min = now.astimezone(timezone.utc)
    time_max = time_min + timedelta(hours=max(1, int(hours)))
    resp = (
        service.freebusy()
        .query(
            body={
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "items": [{"id": calendar_id}],
            }
        )
        .execute()
    )
    cal = (resp.get("calendars", {}) or {}).get(calendar_id, {})
    return [{"start": b.get("start", ""), "end": b.get("end", "")} for b in cal.get("busy", []) or []]
