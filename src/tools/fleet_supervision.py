"""Fleet supervision tools for the hub agent (ADR 0135 D8).

Two read-only answers to "what is the fleet doing", built only from seams that already exist:

* **The roster and its health.** Members come from ``graph.fleet.supervisor.status()`` (through
  ``tools.fleet_diagnostics._roster``, the same tolerant read ``fleet_diagnostics`` resolves
  against) and their health from ``supervisor.member_health()``, the record ``health_sweep``
  keeps. Status refreshes that record through ``supervisor.fresh_member_health``; the
  supervisor owns all probing, the failure threshold and health classification.
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
a display name stay two rows. The two read tools never mutate members. The independently
opt-in restart tool uses approval interrupts and the shared fleet lifecycle ops.
``fleet_dismiss`` uses the member operator routes, only in operator chat turns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any
from urllib.parse import quote

from langchain_core.tools import tool
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt import InjectedState
from langgraph.types import interrupt

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
        # health loop exists, so sweep now (max_age 0). The records are shared with the
        # health loop and the ``fleet:member_health`` watch check, so this read does not
        # bring a threshold of its own: the supervisor counts this pass against the one
        # threshold every sweep uses, and the changes it finds stay claimable by the
        # loop. A first failed check is shown from the record's ``failures`` count
        # (``_health_view``), without reclassifying the member for anyone else. Sweep
        # before status so remote running and version fields see the same refreshed
        # probe cache.
        supervisor.fresh_member_health(0)
        health = {str(h.get("id") or ""): h for h in supervisor.member_health() if isinstance(h, dict)}
    except Exception:  # noqa: BLE001 — a health read failure must not fail the roster answer
        log.exception("[fleet-supervision] health read failed")
        health = {}
    return fd._roster(), health


def _health_view(record: dict[str, Any] | None) -> tuple[str, bool | None, int | None]:
    """``(health, healthy, failed_checks)`` for one member's supervisor health record.

    ``health`` is the supervisor's state verbatim, with one addition: a member the supervisor
    still holds ``healthy`` whose latest check failed is ``failing`` (and not ``healthy``), so
    one failed probe shows in this read while the supervisor's own threshold still decides
    ``wedged`` / ``down``. No record (not swept yet, the host itself, or a member stopped on
    purpose) is ``unknown`` with ``healthy`` None; it is never inferred from ``running``."""
    if not record:
        return "unknown", None, None
    state = str(record.get("state") or "unknown")
    failures = record.get("failures")
    failures = failures if isinstance(failures, int) and not isinstance(failures, bool) and failures > 0 else 0
    if state == "healthy" and failures:
        state = "failing"
    return state, state == "healthy", failures


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
    cannot be read is a ``status: "unreachable"`` row, never a failed call.
    Use ``fleet_dismiss`` when the operator asks to clear a stale item."""
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
        row["health"], row["healthy"], row["failed_checks"] = _health_view(record)
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
    """Bind fleet reads and dismissals (``tools.fleet_diagnostics.enabled``, hub only).

    Reads cover the configured roster; dismissals resolve immutable member ids.
    """

    @tool("fleet_status")
    async def fleet_status_tool() -> dict:
        """Report every fleet member's state: running, healthy, version, model, when its health
        last changed, and how many decisions and waiting tasks it has pending.

        Read-only. Members are listed by their unique ``id``, so two members with the same name
        are separate rows. ``health`` is ``healthy``, ``failing`` (its latest health check
        failed, but not yet enough in a row to count as ``wedged`` or ``down``), ``wedged``,
        ``down`` or ``unknown``; ``failed_checks`` is how many checks in a row have failed.
        ``healthy`` is null when the supervisor has no health record yet, and the counts are
        null for a member that is stopped or could not be read.
        """
        return await fleet_status()

    @tool("fleet_pending")
    async def fleet_pending_tool() -> dict:
        """List what is waiting on the operator across the fleet: each member's pending
        decisions and its tasks in the ``waiting`` state.

        Read-only: it cannot answer a decision or change a task. A member that cannot be read is
        reported with ``status: "unreachable"`` (or ``"stopped"``) and the others still answer.
        Use fleet_dismiss with a reason when the operator asks to clear a stale item.
        """
        return await fleet_pending()

    @tool("fleet_dismiss")
    async def fleet_dismiss_tool(
        member: str, kind: str, item_id: str, reason: str,
        state: Annotated[Any, InjectedState] = None,
    ) -> dict:
        """Dismiss a stale decision or waiting task when the operator asks in chat.

        Supply the immutable member id from fleet_pending, kind (decision or task),
        item_id and a non-empty reason. Cancels decisions without choosing an answer;
        closes tasks with outcome dismissed. Unattended and subagent turns are refused.
        """
        from graph.fleet import member_read
        from graph.middleware.knowledge import _restrict_prior_session_memory
        from graph.middleware.redaction import redact
        from observability.audit import audit_logger
        from tools.directive_tools import _operator_chat_turn

        began = time.monotonic()
        result = {"ok": False, "error": "unreachable"}
        try:
            if not _operator_chat_turn(state):
                result = {"ok": False, "error": "operator_turn_required"}
            elif _restrict_prior_session_memory():
                result = {"ok": False, "error": "operator_only"}
            elif not reason.strip():
                result = {"ok": False, "error": "reason_required"}
            elif kind not in ("decision", "task"):
                result = {"ok": False, "error": "invalid_kind"}
            elif not item_id.strip() or item_id in (".", ".."):
                result = {"ok": False, "error": "invalid_item_id"}
            else:
                roster = await asyncio.to_thread(fd._roster)
                matches = [e for e in roster if e.get("id") == member]
                if len(matches) != 1:
                    result = {"ok": False, "error": "ambiguous_member" if matches else "unknown_member"}
                elif _readable(matches[0]):
                    encoded = quote(item_id, safe="")
                    path = (f"api/decisions/{encoded}/cancel" if kind == "decision"
                            else f"api/tasks/issues/{encoded}/close")
                    payload = {"reason": reason}
                    if kind == "task":
                        # Read the member's authoritative tasks, not the bounded pending preview.
                        body = await member_read._fetch_member_json(fd._slug_for(matches[0]), _TASKS_PATH)
                        if body is None or not isinstance(body.get("issues"), list):
                            return result
                        task = next((t for t in body["issues"] if t.get("id") == item_id), None)
                        if task is None:
                            result = {"ok": False, "error": "not_found"}
                            return result
                        if task.get("status") != "waiting":
                            result = {"ok": False, "error": "task_not_waiting"}
                            return result
                        payload["outcome"] = "dismissed"
                        # The member rechecks under its write lock if the task resumes meanwhile.
                        payload["expected_status"] = "waiting"
                    result = await member_read._post_member_json(fd._slug_for(matches[0]), path, payload)
                    if not result["ok"] and "status_code" in result:
                        result = {**result, "error": "not_found" if result["status_code"] == 404 else "member_error"}
            return result
        except Exception:  # noqa: BLE001
            result = {"ok": False, "error": "unreachable"}
            return result
        finally:
            await asyncio.to_thread(
                audit_logger.log,
                session_id=state.get("session_id", "") if isinstance(state, dict) else "",
                tool="fleet_dismiss",
                args=redact({"member": member, "kind": kind, "id": item_id, "reason": reason}),
                result_summary=str(result),
                duration_ms=int((time.monotonic() - began) * 1000),
                success=result["ok"],
            )

    return [fleet_status_tool, fleet_pending_tool, fleet_dismiss_tool]


def _restart_target(member: str) -> dict[str, Any]:
    """Resolve one local member and read durable operator intent off the event loop."""
    from graph.fleet import supervisor

    # Immutable ids keep checkpoint replay from approving a different same-named member.
    matches = [entry for entry in fd._roster() if entry.get("id") == member]
    if len(matches) != 1:
        return {"ok": False, "error": "ambiguous_member" if matches else "unknown_member"}
    entry = matches[0]
    if _kind(entry) != "local" or not fd._slug_for(entry):
        return {"ok": False, "error": "local_member_required"}
    refusal = supervisor.recovery_refusal(entry["id"])
    if refusal:
        return {"ok": False, "error": refusal}
    return {"ok": True, "id": entry["id"], "running": bool(entry.get("running"))}


def _restart_action(target: dict[str, Any]) -> str:
    """The action a restart call takes on ``target``: ``start`` when its process is dead,
    ``restart`` (stop when idle, then start) when it is running."""
    return "restart" if target.get("running") else "start"


def build_fleet_member_restart_tools(graph_config) -> list:
    """Bind the independently opt-in, hub-only member recovery tool."""
    from graph.workspaces.manager import is_workspace_member

    if not getattr(graph_config, "fleet_member_restart_enabled", False) or is_workspace_member():
        return []

    @tool("fleet_member_restart")
    async def fleet_member_restart_tool(
        member: str, reason: str, state: Annotated[Any, InjectedState] = None,
    ) -> dict:
        """Request approval to start one down local fleet member, or restart it when idle.

        Supply its immutable roster id (from fleet_status) and a reason. Never overrides an
        operator's deliberate stop. Busy or unreachable running members are not restarted.
        """
        from graph.middleware.knowledge import _restrict_prior_session_memory
        from graph.middleware.redaction import redact
        from observability.audit import audit_logger
        from ops import fleet
        from tools.fs_tools import _approved

        began = time.monotonic()
        result = {"ok": False, "error": "approval_required"}
        try:
            if _restrict_prior_session_memory():
                result = {"ok": False, "error": "operator_only"}
                return result
            if not graph_config.fleet_member_restart_enabled or is_workspace_member():
                result = {"ok": False, "error": "disabled"}
                return result
            if not reason.strip():
                result = {"ok": False, "error": "reason_required"}
                return result
            target = await asyncio.to_thread(_restart_target, member)
            if not target["ok"]:
                result = target
                return result
            slug = target["id"]
            decision = interrupt({
                "kind": "approval",
                "title": "Approve fleet member start / restart when idle?",
                "detail": f"Member: {slug}\nReason: {reason}",
                "session_allow": False,
                # What this call would do, for the unattended approval policy (ADR 0124 D5):
                # "start" a member whose process is dead, or "restart" a running one. ADR
                # 0135 D2 puts only "start" in the hub's allow tier.
                "action": _restart_action(target),
            })
            if not _approved(decision):
                result = {"ok": False, "error": "approval_denied"}
                return result
            # Approval may have waited while the member stopped, disappeared or became busy.
            # Re-read by immutable id; never resolve an approved display name to another member.
            target = await asyncio.to_thread(_restart_target, slug)
            if not target["ok"]:
                result = target
                return result
            # An unattended allowance names the one action it covers. If the member came
            # back up since, restarting it is a different action: not covered, not run.
            granted = decision.get("action") if isinstance(decision, dict) and decision.get("unattended") else None
            if granted and granted != _restart_action(target):
                result = {"ok": False, "error": "state_changed"}
                return result
            if target["running"]:
                stopped = await fleet.stop(slug, when_idle=True)
                if stopped.get("stopped") is not True:
                    result = {"ok": False, "error": "stop_failed"}
                    return result
            started = await fleet.start(slug)
            result = {"ok": bool(started.get("running")), "id": slug, "running": bool(started.get("running"))}
            return result
        except GraphBubbleUp:
            raise
        except fleet.RestartNotReady:
            result = {"ok": False, "error": "not_ready"}
            return result
        except Exception:
            log.exception("[fleet-supervision] member recovery failed")
            result = {"ok": False, "error": "restart_failed"}
            return result
        finally:
            # Also records refused calls and approval interrupts. Bubble-up exceptions
            # propagate so the unattended approval policy can rule on the approval.
            await asyncio.to_thread(
                audit_logger.log,
                session_id=(state or {}).get("session_id", ""),
                tool="fleet_member_restart",
                args=redact({"member": member, "reason": reason}),
                result_summary=str(result),
                duration_ms=int((time.monotonic() - began) * 1000),
                success=result["ok"],
            )

    return [fleet_member_restart_tool]
