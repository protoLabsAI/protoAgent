"""Guarded, read-only fleet diagnostics tool (#3170, ADR 0071).

Lets an *explicitly authorized* managing agent inspect a registered fleet member's
bounded log snapshot or one exact A2A task — the peer-support counterpart to the console
drawer (#3169), reusing the member-local diagnostics API (#3168) rather than duplicating
task/log reads.

Trust boundary (ADR 0071). Three properties make this safe to hand a model:

* **Default off.** The tool is bound only when ``fleet.diagnostics.enabled`` is set
  (``fleet_diagnostics_enabled``, default ``False``), so an ordinary agent never sees it.
  Gating happens at bind time in ``tools.lg_tools.get_all_tools`` — a disabled instance
  exposes no surface at all, not a tool that only refuses.
* **Roster-only addressing.** The target is resolved *exclusively* through the configured
  fleet roster (local workspaces + registered remote members). The model names a member;
  it never supplies a host or URL, so the tool cannot be pointed at an arbitrary address
  and an unknown/unregistered member is refused before any request is made.
* **Read-only, through the existing proxy.** The one and only request the tool ever issues
  is a ``GET`` to ``/agents/{slug}/api/diagnostics/*`` on this hub — the authenticated,
  bounded, redacted #3168 endpoints, reached through the ADR 0042 slug proxy that already
  routes to a local peer or a remote member and swaps in the right credential. There is no
  code path here that starts a member, resumes/answers a task or HITL prompt, mutates a
  checkpoint, or writes config.

Failure containment. A stopped / unreachable / slow member is the proxy's case (409 / 502 /
504); a bad line count, an unknown task id, or a missing task store is the member's
(clamped note / 404 / 503). Either way this tool turns the status into a compact, actionable
object instead of an uncaught error. Output is bounded and passed through the shared
credential redactor before it returns — belt-and-suspenders over the server-side redaction,
because a remote member may run a different build.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

import httpx
from langchain_core.tools import tool

log = logging.getLogger("protoagent.tools")

# Read caps. The member-local endpoints (#3168) bound their own responses; these are the
# tool's own guards so the object handed back to the model stays small regardless of what
# a (possibly older / forked) member returns.
_DEFAULT_LINES = 200
_MAX_LINES = 1000
_MAX_OUTPUT_CHARS = 40_000  # hard ceiling on the serialized object returned to the model
_MAX_LEAF_CHARS = 4_000  # per-string cap applied when trimming to fit the ceiling

# Bounded timeouts. The read lane sits just above the proxy's own 20s ``/api`` read timeout
# (graph/fleet/proxy.py) so a stalled member surfaces as the proxy's 504 rather than our
# client giving up first — both map to the same "did not respond in time" answer.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# Test seam: a MockTransport injected here exercises the tool without a live hub. Production
# leaves it None (a normal loopback client).
_TEST_TRANSPORT: httpx.BaseTransport | None = None


def _clamp_lines(lines: object) -> int:
    """Coerce a caller's ``lines`` into ``[1, _MAX_LINES]`` (clamp, never reject)."""
    try:
        value = int(lines)
    except (TypeError, ValueError):
        return _DEFAULT_LINES
    return max(1, min(value, _MAX_LINES))


def _roster() -> list[dict]:
    """Every registered member this hub can address: local workspaces + remote members.

    Records are normalized to ``{id, name, kind}``. Resolution is confined to this set —
    the reserved ``host`` slug (this instance itself) is deliberately absent; the tool
    inspects *members*, not self. Best-effort per half: a missing/broken registry yields an
    empty list rather than raising."""
    out: list[dict] = []
    try:
        from graph.workspaces import manager

        for w in manager.list_workspaces():
            if isinstance(w, dict) and w.get("id"):
                out.append({"id": w["id"], "name": w.get("name") or w["id"], "kind": "local"})
    except Exception:  # noqa: BLE001 — a roster read must never raise into the tool
        log.debug("[fleet-diagnostics] local roster read failed", exc_info=True)
    try:
        from graph.fleet import supervisor

        for r in supervisor.list_remotes():
            if isinstance(r, dict) and r.get("id"):
                out.append({"id": r["id"], "name": r.get("name") or r["id"], "kind": "remote"})
    except Exception:  # noqa: BLE001
        log.debug("[fleet-diagnostics] remote roster read failed", exc_info=True)
    return out


def _resolve(selector: str) -> dict | None:
    """A member record for an id-or-display-name, matched against the roster ONLY.

    Case-insensitive; an exact id wins over a name. Returns ``None`` for anything not in
    the roster — the sole addressing path, so an unknown/unregistered target is unreachable
    by construction (there is no host/URL escape hatch)."""
    sel = (selector or "").strip().lower()
    if not sel:
        return None
    members = _roster()
    return next((m for m in members if m["id"].lower() == sel), None) or next(
        (m for m in members if m["name"].lower() == sel), None
    )


def _hub_base() -> str | None:
    """``http://127.0.0.1:{active_port}`` — this hub, where the ``/agents/{slug}/*`` proxy
    lives. ``None`` when the port isn't known yet (no live server)."""
    from runtime.state import STATE

    port = getattr(STATE, "active_port", None)
    return f"http://127.0.0.1:{int(port)}" if port else None


def _fail(member: str, error: str, *, status: int | None = None, detail: object = None, **extra) -> str:
    """A compact, actionable failure object (never raises into the caller)."""
    payload: dict = {"ok": False, "member": member, "error": error}
    if status is not None:
        payload["status"] = status
    if detail is not None:
        payload["detail"] = detail
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _shrink(obj, budget: int):
    """Recursively cap string leaves to ``budget`` chars — the final guard used to force an
    over-size payload under the output ceiling without producing invalid JSON."""
    if isinstance(obj, str):
        return obj[:budget] + "…[trimmed]" if len(obj) > budget else obj
    if isinstance(obj, list):
        return [_shrink(v, budget) for v in obj]
    if isinstance(obj, dict):
        return {k: _shrink(v, budget) for k, v in obj.items()}
    return obj


def _cap_output(payload: dict) -> str:
    """Serialize ``payload``, forcing it under ``_MAX_OUTPUT_CHARS``.

    Preserves the caller-bounded structure where it can: log lines (the one unbounded-count
    axis the tool controls) are trimmed oldest-first; if a single huge field still overflows,
    string leaves are capped. Either trimming stamps ``output_truncated`` so the model knows
    the view is partial — never a silent cut."""
    s = json.dumps(payload, ensure_ascii=False)
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s
    payload["output_truncated"] = True
    data = payload.get("data")
    lines = data.get("lines") if isinstance(data, dict) else None
    if isinstance(lines, list):
        # Note goes on BEFORE trimming so the loop measures the payload it actually returns —
        # otherwise adding it afterward could nudge the result back over the ceiling. Then drop
        # the oldest lines until it fits (keeping the most recent tail).
        data["output_note"] = "oldest log lines dropped to fit the output cap"
        while lines and len(json.dumps(payload, ensure_ascii=False)) > _MAX_OUTPUT_CHARS:
            lines.pop(0)
    s = json.dumps(payload, ensure_ascii=False)
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s
    # Still over (a task with large text fields): cap every string leaf.
    for budget in (_MAX_LEAF_CHARS, 1_000, 200):
        payload = _shrink(payload, budget)
        s = json.dumps(payload, ensure_ascii=False)
        if len(s) <= _MAX_OUTPUT_CHARS:
            break
    return s


async def _get(path: str, params: dict | None = None) -> httpx.Response | None:
    """GET ``{hub}/{path}`` with the fleet service token. ``None`` signals a transport-level
    failure the caller renders (unreachable / timeout)."""
    base = _hub_base()
    if base is None:
        return None
    from graph.fleet.service_token import resolve_service_token

    headers = {"authorization": f"Bearer {resolve_service_token()}"}
    kwargs: dict = {"timeout": _TIMEOUT}
    if _TEST_TRANSPORT is not None:
        kwargs["transport"] = _TEST_TRANSPORT
    async with httpx.AsyncClient(**kwargs) as client:
        return await client.get(f"{base}/{path}", headers=headers, params=params or {})


def _render(member: str, kind_label: str, resp: httpx.Response) -> str:
    """Turn a diagnostics response into the compact tool object — success or a mapped failure.

    The #3168 endpoints + the ADR 0042 proxy answer with a small, principled set of status
    codes; each becomes an actionable error rather than an uncaught surprise. Server
    truncation/malformed signals ride through in ``data``; the whole thing is redacted."""
    status = resp.status_code
    if status == 200:
        try:
            body = resp.json()
        except ValueError:
            return _fail(member, "malformed response", status=status, detail="member did not return JSON")
        from graph.middleware.redaction import redact

        return _cap_output({"ok": True, "member": member, "kind": kind_label, "data": redact(body)})

    # Non-200 — read a detail if the body carries one, then map the status.
    detail: object = None
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        detail = (resp.text or "")[:300] or None

    mapping = {
        401: "unauthorized",
        403: "unauthorized",
        404: "not found",
        409: "member not running",
        502: "member unreachable",
        503: "member store unavailable",
        504: "member did not respond in time",
    }
    return _fail(member, mapping.get(status, "request failed"), status=status, detail=detail)


@tool
async def fleet_diagnostics(member: str, what: str = "logs", task_id: str = "", lines: int = _DEFAULT_LINES) -> str:
    """Inspect a registered fleet member's recent logs or one exact A2A task (READ-ONLY).

    A peer-support tool for a managing agent: read a sister agent's bounded log tail or one
    task's state/history/output to diagnose it — without shell access. It cannot change
    anything (no starting, resuming, answering, or config edits) and can only reach members
    already in this instance's fleet roster.

    Args:
        member: The member to inspect, by fleet id or display name. Must be a registered
            member — unknown names are refused (the tool cannot target an arbitrary host/URL).
        what: ``"logs"`` (default) for a bounded tail of the member's log ring, or ``"task"``
            to inspect one exact A2A task (``task_id`` required).
        task_id: The exact task id to inspect when ``what="task"``. No wildcards — one task.
        lines: For ``what="logs"``, how many recent log lines to return. Clamped to
            1..1000 (default 200).

    Returns a compact JSON object: ``{"ok": true, "member", "kind", "data": {...}}`` on
    success, where ``data`` is the member's bounded, redacted diagnostics payload (it keeps
    the server's own ``truncated``/``malformed``/``note`` signals). On any failure —
    unknown member, a stopped/unreachable/slow member, an unauthorized read, a missing task
    — it returns ``{"ok": false, "member", "error", ...}`` instead of raising.
    """
    member = (member or "").strip()
    if not member:
        return _fail(member, "no member given", detail="name a registered fleet member to inspect")

    rec = _resolve(member)
    if rec is None:
        known = sorted(m["name"] for m in _roster())
        return _fail(
            member,
            "unknown member",
            detail="not in the fleet roster; the tool only reaches registered members",
            known_members=known,
        )

    slug = quote(rec["id"], safe="")
    mode = (what or "logs").strip().lower()
    if mode in ("logs", "log"):
        path, params, kind_label = f"agents/{slug}/api/diagnostics/logs", {"lines": _clamp_lines(lines)}, "logs"
    elif mode in ("task", "tasks"):
        tid = (task_id or "").strip()
        if not tid:
            return _fail(rec["name"], "task_id required", detail="what='task' needs an exact task_id")
        path, params, kind_label = f"agents/{slug}/api/diagnostics/tasks/{quote(tid, safe='')}", None, "task"
    else:
        return _fail(rec["name"], "invalid 'what'", detail=f"expected 'logs' or 'task', got {what!r}")

    try:
        resp = await _get(path, params=params)
    except httpx.TimeoutException:
        return _fail(rec["name"], "member did not respond in time")
    except httpx.HTTPError as exc:
        # ConnectError / read failure reaching the member through the proxy.
        return _fail(rec["name"], "member unreachable", detail=str(exc))
    if resp is None:
        return _fail(rec["name"], "hub unavailable", detail="fleet hub port is not resolvable on this instance")
    return _render(rec["name"], kind_label, resp)
