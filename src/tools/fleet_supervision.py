"""Fleet read tools for the hub agent: ``fleet_status`` and ``fleet_pending`` (ADR 0135 D8, #5534).

Two read-only answers to "what is the fleet doing", built only from seams that already exist:

* **The roster and its health.** Members come from ``graph.fleet.supervisor.status()`` (through
  ``tools.fleet_diagnostics._roster``, the same tolerant read ``fleet_diagnostics`` resolves
  against) and their health from ``supervisor.member_health()``, the record ``health_sweep``
  keeps. Status refreshes that record through the supervisor's sweep; the supervisor
  owns all probing and health classification.
* **Reading a member.** Pending decisions (``/api/decisions``), waiting tasks
  (``/api/tasks/issues``) and the model (``/api/runtime/status``) are read through
  ``graph.fleet.member_read._fetch_member_json``: its auth swap, its timeout and its failure
  containment. A member whose reads all fail is reported ``unreachable``; the call still answers
  for every other member.
* **Bounds and redaction.** Member-authored text crosses into a model's context, so it is
  redacted with ``graph.middleware.redaction.redact`` and then capped with the
  ``tools.fleet_diagnostics`` limits, in that order, so a cap can never split a secret into the
  output. A trim on this side is signalled with ``tool_truncated``.

Members are reported as a list keyed by their immutable roster ``id``, so two members that share
a display name stay two rows. Both tools are read-only: there is no start, stop, answer or
config path here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import tool

from tools import fleet_diagnostics as fd

log = logging.getLogger(__name__)

# Rows of one kind (decisions, or waiting tasks) reported per member. The member's own list
# routes bound decisions (``limit``) but not tasks, so the cap is held here.
_MAX_ITEMS = fd._MAX_TASK_HISTORY

_DECISIONS_PATH = "api/decisions?state=pending&limit={limit}"
_TASKS_PATH = "api/tasks/issues"
_RUNTIME_PATH = "api/runtime/status"

_DECISION_FIELDS = ("id", "session_id", "agent", "question", "context", "created_at", "expires_at")
_TASK_FIELDS = ("id", "title", "ask", "status", "priority", "assignee", "session_id", "updated_at")


def _roster_and_health() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """The roster plus each member's health record by id. Synchronous (the roster read takes a
    file lock and reads the registry), so callers run it off the event loop."""
    from graph.fleet import supervisor

    try:
        # An on-demand observation must populate the record even before a background
        # health loop exists. One failed probe reports unhealthy for this read; there
        # is no restart or paging policy here. Sweep before status so remote running
        # and version fields see the same refreshed probe cache.
        supervisor.health_sweep(failure_threshold=1)
        health = {str(h.get("id") or ""): h for h in supervisor.member_health() if isinstance(h, dict)}
    except Exception:  # noqa: BLE001 — a health read failure must not fail the roster answer
        log.exception("[fleet-supervision] health read failed")
        health = {}
    return fd._roster(), health


def _kind(entry: dict[str, Any]) -> str:
    if entry.get("host"):
        return "host"
    return "remote" if entry.get("remote") else "local"


def _base_row(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(entry.get("id") or "").strip(),
        "name": fd._display(entry),
        "kind": _kind(entry),
        "running": bool(entry.get("running")),
    }


def _readable(entry: dict[str, Any]) -> bool:
    """Whether to read this member at all. A local member the supervisor reports stopped is not
    read (there is nothing listening); a remote is always tried, because its ``running`` is only
    the last cached probe."""
    if not fd._slug_for(entry):
        return False
    return bool(entry.get("running")) or bool(entry.get("remote"))


async def _read(slug: str, path: str) -> dict | None:
    """One member read through the shared helper. It already contains every failure as None;
    the guard here only keeps a patched or future helper from failing the whole call."""
    from graph.fleet import member_read

    try:
        return await member_read._fetch_member_json(slug, path)
    except Exception:  # noqa: BLE001
        return None


def _pending_decisions(body: dict | None) -> tuple[list[dict[str, Any]] | None, int | None]:
    """``(pending decision rows, total pending)`` from a member's ``/api/decisions`` answer, or
    ``(None, None)`` when that read failed."""
    if body is None:
        return None, None
    rows = body.get("decisions")
    rows = [d for d in rows if isinstance(d, dict) and d.get("state", "pending") == "pending"] if isinstance(rows, list) else []
    total = body.get("pending")
    if not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
        total = len(rows)
    return rows, total


def _waiting_tasks(body: dict | None) -> list[dict[str, Any]] | None:
    """The ``waiting`` task rows from a member's ``/api/tasks/issues`` answer, or None when that
    read failed (unreachable, or the member has no task store)."""
    if body is None:
        return None
    rows = body.get("issues")
    if not isinstance(rows, list):
        return []
    return [i for i in rows if isinstance(i, dict) and str(i.get("status") or "").lower() == "waiting"]


def _cap(value: Any, flag: list[bool]) -> Any:
    """Cap every string in an already-redacted value to the fleet-diagnostics field cap."""
    if isinstance(value, str):
        if len(value) > fd._TOOL_TEXT_CAP:
            flag.append(True)
            return value[: fd._TOOL_TEXT_CAP]
        return value
    if isinstance(value, dict):
        return {k: _cap(v, flag) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap(v, flag) for v in value]
    return value


def _decision_view(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in _DECISION_FIELDS if row.get(k) not in (None, "")}
    options = row.get("options")
    if isinstance(options, list) and options:
        out["options"] = [
            {k: o.get(k) for k in ("id", "label") if o.get(k)} for o in options[: fd._MAX_SUGGESTIONS] if isinstance(o, dict)
        ]
    return out


def _task_view(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in _TASK_FIELDS if row.get(k) not in (None, "")}


def _bounded(rows: list[dict[str, Any]], view, flag: list[bool]) -> list[dict[str, Any]]:
    """Member rows → the compact view, redacted, THEN capped (rows and fields)."""
    from graph.middleware.redaction import redact

    if len(rows) > _MAX_ITEMS:
        flag.append(True)
        rows = rows[:_MAX_ITEMS]
    return _cap(redact([view(r) for r in rows]), flag)


async def fleet_pending() -> dict[str, Any]:
    """Pending decisions and waiting tasks for every roster member. Read-only; a member that
    cannot be read is a ``status: "unreachable"`` row, never a failed call."""
    roster = await asyncio.to_thread(fd._roster)

    async def one(entry: dict[str, Any]) -> dict[str, Any]:
        row = _base_row(entry)
        if not _readable(entry):
            row["status"] = "stopped"
            return row
        slug = fd._slug_for(entry)
        decisions_body, tasks_body = await asyncio.gather(
            _read(slug, _DECISIONS_PATH.format(limit=_MAX_ITEMS)),
            _read(slug, _TASKS_PATH),
        )
        if decisions_body is None and tasks_body is None:
            row["status"] = "unreachable"
            return row
        row["status"] = "ok"
        flag: list[bool] = []
        decisions, total = _pending_decisions(decisions_body)
        tasks = _waiting_tasks(tasks_body)
        # None (not 0, not []) when one of the two reads failed: "could not read" must not
        # look like "nothing waiting".
        row["pending_decisions"] = total
        row["decisions"] = None if decisions is None else _bounded(decisions, _decision_view, flag)
        if total is not None and total > len(row["decisions"]):
            flag.append(True)
        row["waiting_tasks"] = None if tasks is None else len(tasks)
        row["tasks"] = None if tasks is None else _bounded(tasks, _task_view, flag)
        if flag:
            row["tool_truncated"] = True
        return row

    members = list(await asyncio.gather(*(one(e) for e in roster)))
    return {
        "ok": True,
        "members": members,
        "pending_decisions": sum(m.get("pending_decisions") or 0 for m in members),
        "waiting_tasks": sum(m.get("waiting_tasks") or 0 for m in members),
        "unreachable": [m["id"] for m in members if m.get("status") == "unreachable"],
    }


async def fleet_status() -> dict[str, Any]:
    """One row per roster member: running, health, version, model and pending counts."""
    from graph.middleware.redaction import redact

    roster, health = await asyncio.to_thread(_roster_and_health)

    async def one(entry: dict[str, Any]) -> dict[str, Any]:
        row = _base_row(entry)
        record = health.get(row["id"])
        # The supervisor's health record, verbatim. No record (not swept yet, the host itself,
        # or a member stopped on purpose) is "unknown" — never inferred from ``running``.
        row["health"] = str(record.get("state") or "unknown") if record else "unknown"
        row["healthy"] = (row["health"] == "healthy") if record else None
        row["health_changed_at"] = record.get("changed_at") if record else None
        row["version"] = str(entry.get("version") or "")
        row["model"] = None
        row["pending_decisions"] = None
        row["waiting_tasks"] = None
        if not _readable(entry):
            row["reachable"] = None
            return row
        slug = fd._slug_for(entry)
        runtime, decisions_body, tasks_body = await asyncio.gather(
            _read(slug, _RUNTIME_PATH),
            _read(slug, _DECISIONS_PATH.format(limit=1)),
            _read(slug, _TASKS_PATH),
        )
        row["reachable"] = not (runtime is None and decisions_body is None and tasks_body is None)
        model = (runtime or {}).get("model")
        name = model.get("name") if isinstance(model, dict) else None
        if isinstance(name, str) and name:
            row["model"] = str(redact(name))[: fd._TOOL_TEXT_CAP]
        row["pending_decisions"] = _pending_decisions(decisions_body)[1]
        tasks = _waiting_tasks(tasks_body)
        row["waiting_tasks"] = None if tasks is None else len(tasks)
        return row

    members = list(await asyncio.gather(*(one(e) for e in roster)))
    return {"ok": True, "members": members}


def build_fleet_supervision_tools() -> list:
    """Bind ``fleet_status`` and ``fleet_pending`` (``tools.fleet_diagnostics.enabled``, hub only).

    Thin adapters over the two reads above. Neither takes an argument, so there is no host, URL
    or member name for the model to point anywhere: both answer for the configured roster.
    """

    @tool("fleet_status")
    async def fleet_status_tool() -> dict:
        """Report every fleet member's state: running, healthy, version, model, when its health
        last changed, and how many decisions and waiting tasks it has pending.

        Read-only. Members are listed by their unique ``id``, so two members with the same name
        are separate rows. ``healthy`` is null when the supervisor has no health record yet, and
        the counts are null for a member that is stopped or could not be read.
        """
        return await fleet_status()

    @tool("fleet_pending")
    async def fleet_pending_tool() -> dict:
        """List what is waiting on the operator across the fleet: each member's pending
        decisions and its tasks in the ``waiting`` state.

        Read-only: it cannot answer a decision or change a task. A member that cannot be read is
        reported with ``status: "unreachable"`` (or ``"stopped"``) and the others still answer.
        """
        return await fleet_pending()

    return [fleet_status_tool, fleet_pending_tool]
