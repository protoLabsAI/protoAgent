"""Guarded read-only fleet diagnostics tool (#3170, ADR 0071).

A managing agent — WHEN the operator has explicitly enabled it
(``tools.fleet_diagnostics_enabled``, default OFF) — can inspect a registered fleet
member's bounded log snapshot or one exact A2A task by id. Nothing here mutates
anything: it is two GETs against the member-local diagnostics surface (#3168), reached
ONLY through the hub's canonical ``/agents/<slug>/api/diagnostics/*`` reverse proxy.

Trust boundary (ADR 0071):

* **Roster-only addressing.** The target is resolved through the configured fleet
  roster (``graph.fleet.supervisor.status``); an id/name that is not a registered
  member is refused. The tool NEVER builds a member host/port/URL of its own — it dials
  the hub loopback and the one proxy sub-path, and the task id is percent-encoded, so a
  caller can neither point it at an arbitrary address (no SSRF) nor escape the path
  (no traversal). No alternate host/URL path is ever constructed.
* **Operator-authenticated.** It presents the fleet service token (ADR 0089), which the
  hub accepts as the operator tier and swaps for the member's own credential at the
  proxy. The diagnostics surface is operator-only and denied to the federation tier.
* **Read-only.** GET only, against ``/api/diagnostics/logs`` and
  ``/api/diagnostics/tasks/{id}``. It cannot start a member, resume/answer a task or a
  HITL prompt, mutate a checkpoint, or change configuration.
* **Bounded + redacted.** The member already truncates and redacts (#3168); this layer
  keeps the output bounded (a caller line cap + a hard output ceiling), preserves the
  server's ``truncated`` / ``malformed`` / ``note`` signals verbatim, and re-runs
  credential redaction as defense-in-depth before returning.

Default OFF: :func:`build_fleet_diagnostics_tools` returns ``[]`` unless the operator
opts in (the ``build_onboard_tools`` pattern), so an ordinary agent never sees the tool
— not one that only refuses. It is also deliberately kept OFF the operator-MCP bus; that
exposure is a separate security review (see ``runtime.operator_mcp_tools``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

log = logging.getLogger("protoagent.tools")

# The tool's stable identifier — referenced by the config gate, the never-expose set in
# ``runtime.operator_mcp_tools``, and the tests, so it is spelled in exactly one place.
FLEET_DIAGNOSTICS_TOOL_NAME = "fleet_diagnostics"

# Caller-bounded line selector + hard output ceilings. The member's own reader (#3168)
# already clamps ``lines`` and caps history/artifacts/text; these are the tool-side bound
# so a single call can never blow the model's context budget.
_DEFAULT_LINES = 200
_MAX_LINES = 1000
_MAX_TEXT_CHARS = 2000  # per log message / per task text field
_MAX_HISTORY_ROWS = 30
_MAX_ARTIFACT_ROWS = 20
_MAX_OUTPUT_CHARS = 24_000
_REQUEST_TIMEOUT = 20.0  # the proxy's own view/read lane is 20s (#2590)


def _err(error: str, member: str, **extra: Any) -> str:
    """A compact, actionable failure object. The tool NEVER raises — every failure mode
    (unknown member, unreachable, timeout, unauthorized, missing task, malformed) is one
    of these strings the model can read and act on."""
    payload: dict[str, Any] = {"ok": False, "error": error, "member": member}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _clamp_lines(lines: Any) -> int:
    """Coerce a caller's ``lines`` into ``[1, _MAX_LINES]`` (the caller-bounded selector).
    Out-of-range clamps rather than rejects; the member reader may clamp further and says
    so in its ``note``, which we preserve."""
    try:
        value = int(lines)
    except (TypeError, ValueError):
        return _DEFAULT_LINES
    return max(1, min(value, _MAX_LINES))


def _resolve_member(member: str) -> tuple[dict | None, list[dict]]:
    """The roster entry whose immutable id or display name matches ``member``, plus the
    full roster. The roster (host + local workspaces + remote members) is THE only
    addressing source (r2): a non-member returns ``(None, roster)`` and is refused."""
    from graph.fleet import supervisor

    roster = supervisor.status()
    ident = (member or "").strip()
    if not ident:
        return None, roster
    for entry in roster:  # exact id or exact display name
        if str(entry.get("id")) == ident or str(entry.get("name")) == ident:
            return entry, roster
    low = ident.lower()  # case-insensitive display-name convenience
    for entry in roster:
        if str(entry.get("name", "")).lower() == low:
            return entry, roster
    return None, roster


def _truncate(value: Any) -> Any:
    """Cap one text field, appending a legible marker when it cut."""
    if isinstance(value, str) and len(value) > _MAX_TEXT_CHARS:
        return value[:_MAX_TEXT_CHARS] + f"…[+{len(value) - _MAX_TEXT_CHARS} chars]"
    return value


def _shape_logs(body: dict, slug: str) -> dict:
    """The member's bounded log tail, re-bounded for the model. Preserves ``enabled`` /
    ``capacity`` / ``returned`` and the server ``note`` (its clamp / disabled signal)."""
    lines = body.get("lines")
    text_cut = False
    if isinstance(lines, list):
        shaped: list[Any] = []
        for rec in lines[-_MAX_LINES:]:
            if isinstance(rec, dict) and isinstance(rec.get("message"), str):
                if len(rec["message"]) > _MAX_TEXT_CHARS:
                    text_cut = True
                shaped.append({**rec, "message": _truncate(rec["message"])})
            else:
                shaped.append(rec)
        lines = shaped
    out: dict[str, Any] = {
        "ok": True,
        "member": slug,
        "view": "logs",
        "enabled": body.get("enabled"),
        "capacity": body.get("capacity"),
        "returned": body.get("returned"),
        "lines": lines if lines is not None else [],
    }
    if body.get("note"):  # server clamp / buffer-disabled signal — preserve verbatim
        out["note"] = body["note"]
    if text_cut:
        out["line_text_truncated"] = True
    return out


def _shape_task(body: dict, slug: str, task_id: str) -> dict:
    """One A2A task, re-bounded for the model. Preserves the member's own ``truncated`` /
    ``malformed`` signals (r4) so a partial/degraded read is never silently clean, and adds
    to ``truncated`` whenever THIS layer trims history/artifacts further than the member did."""
    history_full = body.get("history") if isinstance(body.get("history"), list) else []
    artifacts_full = body.get("artifacts") if isinstance(body.get("artifacts"), list) else []
    out: dict[str, Any] = {
        "ok": True,
        "member": slug,
        "view": "task",
        "task_id": body.get("task_id", task_id),
        "context_id": body.get("context_id"),
        "state": body.get("state"),
        "last_updated": body.get("last_updated"),
        "status_message": _truncate(body.get("status_message", "")),
        "accumulated_text": _truncate(body.get("accumulated_text", "")),
        "history": [
            {**h, "text": _truncate(h.get("text", ""))} if isinstance(h, dict) else h
            for h in history_full[:_MAX_HISTORY_ROWS]
        ],
        "artifacts": [
            {**a, "text": _truncate(a.get("text", ""))} if isinstance(a, dict) else a
            for a in artifacts_full[:_MAX_ARTIFACT_ROWS]
        ],
    }
    trunc = set(body.get("truncated") if isinstance(body.get("truncated"), list) else [])
    if len(history_full) > _MAX_HISTORY_ROWS:
        trunc.add("history")
    if len(artifacts_full) > _MAX_ARTIFACT_ROWS:
        trunc.add("artifacts")
    if trunc:
        out["truncated"] = sorted(trunc)
    malformed = body.get("malformed") if isinstance(body.get("malformed"), list) else []
    if malformed:
        out["malformed"] = list(malformed)
    return out


def _finalize(payload: dict) -> str:
    """Redact (defense-in-depth), then serialize under the hard output ceiling. If still
    over, drop list rows (oldest logs/history first, newest artifacts last) until it fits,
    flagging ``output_truncated`` — the JSON stays valid and bounded. Rows are dropped in
    estimated batches so a member that returns thousands of lines converges in a few passes,
    not one costly re-serialize per row."""
    from graph.middleware.redaction import redact

    safe = redact(payload)
    text = json.dumps(safe, ensure_ascii=False)
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    safe["output_truncated"] = True
    for key, drop_front in (("lines", True), ("history", True), ("artifacts", False)):
        seq = safe.get(key)
        if not isinstance(seq, list):
            continue
        while seq:
            text = json.dumps(safe, ensure_ascii=False)
            if len(text) <= _MAX_OUTPUT_CHARS:
                return text
            over = len(text) - _MAX_OUTPUT_CHARS
            per_row = max(1, len(text) // len(seq))  # avg serialized bytes per remaining row
            drop = max(1, min(len(seq), over // per_row + 1))
            for _ in range(drop):
                if seq:
                    seq.pop(0 if drop_front else -1)
    text = json.dumps(safe, ensure_ascii=False)
    for tk in ("accumulated_text", "status_message"):  # last resort: hard-trim the big text fields
        if len(text) <= _MAX_OUTPUT_CHARS:
            break
        if isinstance(safe.get(tk), str):
            safe[tk] = safe[tk][:200] + "…[truncated]"
            text = json.dumps(safe, ensure_ascii=False)
    return text


def _detail(resp: Any) -> str:
    """The member's own failure detail (from a JSON ``detail`` field, else short text)."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except (ValueError, AttributeError):
        pass
    text = (getattr(resp, "text", "") or "").strip()
    return text[:200]


async def _fetch(slug: str, subpath: str, params: dict, *, transport=None) -> tuple[Any, str | None]:
    """GET the hub's ``/agents/<slug>/api/diagnostics/<subpath>`` proxy path, authenticated
    with the fleet service token. Returns ``(response, None)`` or ``(None, error_json)`` —
    the second element is already a finished tool result for the connection-level failures
    (host port unknown, proxy unreachable, timeout)."""
    import httpx

    from graph.fleet.service_token import resolve_service_token
    from runtime.state import STATE

    port = getattr(STATE, "active_port", None)
    if not port:
        # No hub port ⇒ no reverse proxy to reach; refuse rather than invent a target.
        return None, _err("host_unavailable", slug, detail="hub port is unknown; cannot reach the fleet proxy")
    # The task id is fully percent-encoded so it can never add path segments (r2). The slug
    # is an opaque roster id, encoded for the same reason. We only ever dial loopback.
    url = f"http://127.0.0.1:{port}/agents/{quote(slug, safe='')}/api/diagnostics/{subpath}"
    headers = {"Authorization": f"Bearer {resolve_service_token()}"}
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, transport=transport) as client:
            resp = await client.get(url, params=params, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return None, _err("unreachable", slug, detail="the fleet proxy is not reachable")
    except (httpx.ReadTimeout, httpx.TimeoutException):
        return None, _err("timeout", slug, detail="the member did not respond in time")
    except httpx.HTTPError as exc:
        return None, _err("request_failed", slug, detail=str(exc))
    return resp, None


def _map_status(resp: Any, slug: str, *, task_id: str = "") -> str:
    """Map a non-200 diagnostics response to a compact, actionable failure (r5)."""
    status = resp.status_code
    detail = _detail(resp)
    if status in (401, 403):
        return _err("unauthorized", slug, status=status, detail=detail or "operator credential rejected")
    if status == 404:
        return _err("not_found", slug, status=status, detail=detail or "no such task on this member", task_id=task_id)
    if status == 409:
        return _err("not_running", slug, status=status, detail=detail or "member is not running")
    if status == 502:
        return _err("unreachable", slug, status=status, detail=detail or "member is not reachable")
    if status == 504:
        return _err("timeout", slug, status=status, detail=detail or "member did not respond in time")
    if status == 503:
        return _err("unavailable", slug, status=status, detail=detail or "diagnostics not available on this member")
    return _err("member_error", slug, status=status, detail=detail or f"member returned HTTP {status}")


async def run_fleet_diagnostics(
    member: str,
    view: str = "logs",
    lines: int = _DEFAULT_LINES,
    task_id: str = "",
    *,
    transport=None,
) -> str:
    """The tool body, factored out so tests can drive it with a mock httpx transport.
    Returns a compact JSON string in EVERY case — success or a typed failure — and never
    raises into the turn (r5/r6): the whole path is wrapped so even an unexpected fault
    becomes an actionable object."""
    try:
        return await _run_fleet_diagnostics(member, view, lines, task_id, transport=transport)
    except Exception as exc:  # noqa: BLE001 — a tool must answer, never raise into the turn
        log.exception("[fleet-diagnostics] unexpected failure")
        return _err("tool_error", str(member), detail=str(exc))


async def _run_fleet_diagnostics(member, view, lines, task_id, *, transport=None) -> str:
    try:
        entry, roster = await asyncio.to_thread(_resolve_member, member)
    except Exception as exc:  # noqa: BLE001 — a roster read failure is an answer, not a 500
        log.warning("[fleet-diagnostics] roster resolution failed: %s", exc)
        return _err("roster_unavailable", member, detail="could not read the fleet roster")

    if entry is None:
        # Unknown/unregistered member — refused. No fabricated target is ever dialed (r2).
        available = [str(e.get("name") or e.get("id")) for e in roster][:20]
        return _err("unknown_member", member, detail="not a registered fleet member", available_members=available)

    slug = str(entry.get("id"))
    what = (view or "logs").strip().lower()

    if what in ("logs", "log"):
        params = {"lines": str(_clamp_lines(lines))}
        resp, early = await _fetch(slug, "logs", params, transport=transport)
        if early is not None:
            return early
        if resp.status_code != 200:
            return _map_status(resp, slug)
        try:
            body = resp.json()
        except ValueError:
            return _err("bad_response", slug, detail="member returned non-JSON")
        if not isinstance(body, dict):
            return _err("bad_response", slug, detail="member returned an unexpected shape")
        return _finalize(_shape_logs(body, slug))

    if what in ("task", "tasks"):
        tid = (task_id or "").strip()
        if not tid:
            return _err("task_id_required", slug, detail="view='task' requires a task_id")
        resp, early = await _fetch(slug, f"tasks/{quote(tid, safe='')}", {}, transport=transport)
        if early is not None:
            return early
        if resp.status_code != 200:
            return _map_status(resp, slug, task_id=tid)
        try:
            body = resp.json()
        except ValueError:
            return _err("bad_response", slug, detail="member returned non-JSON", task_id=tid)
        if not isinstance(body, dict):
            return _err("bad_response", slug, detail="member returned an unexpected shape", task_id=tid)
        return _finalize(_shape_task(body, slug, tid))

    return _err("bad_view", slug, detail=f"view must be 'logs' or 'task', got {view!r}")


def build_fleet_diagnostics_tools(graph_config) -> list:
    """Return the guarded fleet-diagnostics tool, or ``[]`` when disabled (the default).

    Mirrors :func:`tools.onboard_tools.build_onboard_tools`: the exposure decision is the
    operator's, made in config (``tools.fleet_diagnostics_enabled``, default OFF), so an
    un-opted-in instance gets no tool at all rather than a tool that only refuses. This is
    the SOLE model-exposure gate — an ordinary agent never sees it (r3)."""
    if not bool(getattr(graph_config, "tools_fleet_diagnostics_enabled", False)):
        return []

    from langchain_core.tools import tool

    @tool
    async def fleet_diagnostics(member: str, view: str = "logs", lines: int = _DEFAULT_LINES, task_id: str = "") -> str:
        """Inspect a registered fleet member's diagnostics — READ ONLY.

        Retrieves, for one member you name, either a bounded tail of its log ring or one
        exact A2A task by id, by calling the member's operator-authenticated diagnostics
        API (#3168) through the hub. It cannot change anything: it can't start or stop a
        member, resume or answer a task or a human-input prompt, edit a checkpoint, or
        change configuration. Use it to diagnose a sister agent that is stuck, silent, or
        misbehaving before deciding what to do about it.

        Args:
            member: The fleet member to inspect, by its roster id or display name. Must be
                a currently registered member — unknown names are refused (nothing else is
                dialed). Run without knowing the roster and the error lists valid members.
            view: ``"logs"`` (default) for the log tail, or ``"task"`` for one A2A task.
            lines: For ``view="logs"``, how many recent log lines to return
                (1–1000, default 200). The member may return fewer and will say so.
            task_id: For ``view="task"``, the exact A2A task id to inspect (required).

        Returns a compact JSON object: for logs, ``{ok, member, view, enabled, capacity,
        returned, lines[, note]}``; for a task, ``{ok, member, view, task_id, state,
        status_message, history, artifacts, accumulated_text[, truncated, malformed]}``.
        On failure it returns ``{ok: false, error, member, detail, …}`` — e.g.
        ``unknown_member``, ``unreachable``, ``timeout``, ``unauthorized``, ``not_found``
        — never an exception. Output is bounded; the member's own truncation/redaction and
        malformed-row signals are preserved.
        """
        return await run_fleet_diagnostics(member, view=view, lines=lines, task_id=task_id)

    return [fleet_diagnostics]
