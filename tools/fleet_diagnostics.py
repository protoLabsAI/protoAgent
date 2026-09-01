"""Guarded read-only fleet-diagnostics tool (ADR 0071, #3170).

An explicitly authorized MANAGING agent (typically the hub's lead agent) inspects a
REGISTERED fleet member's bounded log snapshot or one exact A2A task through that member's
own operator-authenticated ``/api/diagnostics`` surface (#3168). Nothing here re-implements
a task/log read — it reuses the member endpoint the console drawer (#3169) and every fleet
member already serve.

Trust boundary (ADR 0071):

* **Default OFF.** The tool binds only when ``fleet.diagnostics.enabled`` is set on the
  config, so an ordinary agent turn never sees it. Exposing a fleet-reaching read to the
  model is the operator's explicit choice; the #3168 endpoints exist regardless.
* **Roster-only addressing.** The target resolves EXCLUSIVELY through the configured fleet
  roster (``graph.fleet.supervisor.status``). A member the roster does not know cannot be
  reached, and the model never supplies a host/URL — there is no alternate path to a
  member, only the loopback port / registered remote URL the hub already owns. The task id
  is url-quoted to a single path segment so a crafted id can't escape ``/api/diagnostics/``.
* **Read-only.** The only verbs are two GETs against ``/api/diagnostics/*``. The tool
  cannot start a member, resume/answer a task or HITL prompt, mutate a checkpoint, or write
  config — those surfaces are simply never addressed.

Containment. A stopped/unreachable/slow member, an unauthorized response, or a missing task
each becomes a COMPACT structured failure (``{"ok": false, "error": ...}``) rather than an
uncaught exception, so the model gets an actionable answer, never a stack trace.

Boundedness + redaction. The member already bounds and redacts its response (#3168); this
tool re-redacts at its own boundary (defense in depth against an older member) and re-bounds
the output so a long task history can't blow the model's context — while PRESERVING the
server's own ``truncated`` / ``malformed`` signals verbatim.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

# Diagnostics endpoints on a member (mounted by operator_api.diagnostics_routes, #3168).
_LOGS_PATH = "/api/diagnostics/logs"
_TASK_PATH = "/api/diagnostics/tasks/{task_id}"

# Caller-bounded line selector. The member's ring buffer + endpoint clamp independently
# (operator_api.diagnostics_routes._MAX_LINES); this is the tool-side floor/ceiling so an
# absurd request is normalized before it ever leaves.
_DEFAULT_LINES = 200
_MAX_LINES = 1000

# HTTP timeouts — a member that accepts then stalls must not park the turn.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 15.0

# Tool-side output bounds (compact structured output). The member caps each text field at
# 20k and history at 50 rows — up to ~1MB — which is not "compact", so the tool re-caps.
_TOOL_TEXT_CAP = 4000
_MAX_OUTPUT_CHARS = 40_000
# How many roster names to name back on an unknown-member miss, so the model can correct.
_MAX_SUGGESTIONS = 25

# Member HTTP status → the compact error code the model sees. Everything the proxy /
# diagnostics endpoints can answer with is mapped; anything else falls through to http_error.
_STATUS_ERRORS = {
    401: "unauthorized",
    403: "unauthorized",
    404: "not_found",
    409: "not_running",
    502: "unreachable",
    503: "unavailable",
    504: "timeout",
}
_DEFAULT_DETAILS = {
    "unauthorized": "member rejected the diagnostics credential",
    "not_found": "no such task on this member",
    "not_running": "member is not running",
    "unreachable": "member is not reachable",
    "unavailable": "diagnostics is not available on this member",
    "timeout": "member did not respond in time",
    "request_failed": "member request failed",
    "http_error": "member returned an error",
}


class _Target:
    """A resolved member: base URL + the auth headers to reach it. Never carries a
    model-supplied host — always the roster's own loopback port or registered remote URL."""

    __slots__ = ("member", "base", "headers")

    def __init__(self, member: str, base: str, headers: dict[str, str]) -> None:
        self.member = member
        self.base = base
        self.headers = headers


def _fail(error: str, detail: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": error, "detail": detail}
    if extra:
        out.update(extra)
    return out


# ── roster resolution (roster-only addressing, r2) ────────────────────────────


def _roster() -> list[dict[str, Any]]:
    """The configured fleet roster (host + local members + remotes). Never raises — a
    broken registry is a diagnostics answer, not a 500."""
    from graph.fleet import supervisor

    try:
        return [e for e in (supervisor.status() or []) if isinstance(e, dict)]
    except Exception:  # noqa: BLE001 — a registry read failure must not crash the tool
        log.exception("[fleet-diagnostics] roster read failed")
        return []


def _display(entry: dict[str, Any]) -> str:
    return str(entry.get("name") or entry.get("id") or "").strip()


def _match(roster: list[dict[str, Any]], member: str) -> dict[str, Any] | None:
    """The roster entry a caller's ``member`` names, or None. Exact id first, then a
    case-insensitive name/label match — the model addresses members by display name."""
    want = (member or "").strip()
    if not want:
        return None
    for e in roster:
        if str(e.get("id") or "") == want:
            return e
    wl = want.lower()
    for e in roster:
        if str(e.get("name") or "").strip().lower() == wl or str(e.get("label") or "").strip().lower() == wl:
            return e
    return None


def _resolve(member: str) -> _Target | dict[str, Any]:
    """Resolve ``member`` to a ``_Target``, or a compact failure dict. Roster-exclusive: an
    unknown or stopped member never yields a target, and no host/URL comes from the model."""
    roster = _roster()
    entry = _match(roster, member)
    if entry is None:
        names = [n for n in (_display(e) for e in roster) if n][:_MAX_SUGGESTIONS]
        return _fail(
            "unknown_member",
            f"{(member or '').strip()!r} is not a registered fleet member",
            {"available": names},
        )
    label = _display(entry) or (member or "").strip()

    if entry.get("remote"):
        # A remote member is reached at its registered URL with its own stored bearer (never
        # the fleet service token — that credential is loopback-only, ADR 0089).
        from graph.fleet import supervisor

        rec = supervisor.remote_for_slug(entry.get("id")) or {}
        url = str(rec.get("url") or entry.get("url") or "").strip().rstrip("/")
        if not url:
            return _fail("not_running", f"remote member {label!r} has no registered URL")
        headers: dict[str, str] = {}
        token = rec.get("token")
        if token:
            headers["authorization"] = f"Bearer {token}"
        return _Target(label, url, headers)

    # A host / local peer is reached on loopback with the fleet service token — the
    # credential every member accepts as the operator tier (ADR 0089 D3).
    if not entry.get("running"):
        return _fail("not_running", f"member {label!r} is not running")
    port = entry.get("port")
    if not port:
        return _fail("not_running", f"member {label!r} has no reachable address")
    from graph.fleet.service_token import resolve_service_token

    return _Target(label, f"http://127.0.0.1:{port}", {"authorization": f"Bearer {resolve_service_token()}"})


# ── the member call (failure containment, r5) ─────────────────────────────────


def _make_client():
    """The httpx client used for the member call. A seam so tests can drive a MockTransport
    without a live fleet."""
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT))


def _server_detail(resp) -> str | None:
    """The member's own ``detail`` string on an error response, if any — more actionable than
    a generic message. Read defensively (a non-JSON error body is fine)."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"][:_TOOL_TEXT_CAP]
    return None


def _shape(resp) -> dict[str, Any]:
    """A member HTTP response → a normalized result: ``{"ok": True, "body": ...}`` or a
    compact failure. Every non-2xx status is a structured answer, never an exception."""
    status = resp.status_code
    if status >= 400:
        error = _STATUS_ERRORS.get(status, "http_error")
        detail = _server_detail(resp) or _DEFAULT_DETAILS.get(error, f"member returned HTTP {status}")
        return _fail(error, detail, {"status": status})
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON 200 is a malformed member, not a crash
        return {"ok": True, "status": status, "body": {"malformed": ["response_not_json"], "text": (resp.text or "")[:_TOOL_TEXT_CAP]}}
    return {"ok": True, "status": status, "body": body}


async def _fetch(target: _Target, path: str, params: dict[str, str]) -> dict[str, Any]:
    """GET ``<base><path>`` on the member, returning a normalized result. Every network
    failure mode becomes a compact ``{"ok": false, "error": ...}`` — never an exception."""
    import httpx

    url = f"{target.base}{path}"
    client = _make_client()
    try:
        resp = await client.get(url, headers=target.headers, params=params)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return _fail("unreachable", "member is not reachable")
    except httpx.TimeoutException:
        # Covers ReadTimeout/PoolTimeout — the member accepted then went silent.
        return _fail("timeout", "member did not respond in time")
    except httpx.HTTPError as exc:
        return _fail("request_failed", f"member request failed: {type(exc).__name__}")
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    return _shape(resp)


# ── output shaping (bounded + redacted, r4) ───────────────────────────────────


def _clamp_lines(lines: Any) -> tuple[int, str | None]:
    """Coerce a caller's ``lines`` into range, returning ``(value, note)``. Out-of-range is
    clamped, not rejected — a diagnostics read returns the closest useful answer."""
    try:
        value = int(lines)
    except (TypeError, ValueError):
        return _DEFAULT_LINES, f"invalid lines={lines!r}; using {_DEFAULT_LINES}"
    if value < 1:
        return 1, f"lines={value} below minimum; using 1"
    if value > _MAX_LINES:
        return _MAX_LINES, f"lines={value} above maximum; using {_MAX_LINES}"
    return value, None


def _bound_body(body: Any) -> Any:
    """Re-cap the member's already-redacted body for compactness WITHOUT disturbing its own
    ``truncated`` / ``malformed`` signals. Marks ``tool_truncated`` when the tool trims."""
    if not isinstance(body, dict):
        return body
    trimmed = False
    for key in ("accumulated_text", "status_message"):
        val = body.get(key)
        if isinstance(val, str) and len(val) > _TOOL_TEXT_CAP:
            body[key] = val[:_TOOL_TEXT_CAP]
            trimmed = True
    for key in ("history", "artifacts"):
        seq = body.get(key)
        if isinstance(seq, list):
            for row in seq:
                if isinstance(row, dict) and isinstance(row.get("text"), str) and len(row["text"]) > _TOOL_TEXT_CAP:
                    row["text"] = row["text"][:_TOOL_TEXT_CAP]
                    trimmed = True
    if trimmed:
        body["tool_truncated"] = True
    return body


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _finalize(payload: dict[str, Any]) -> str:
    """Serialize, enforcing the overall output bound by dropping trailing collection entries
    (deterministic) before falling back to a compact stub. The returned string is always
    within ``_MAX_OUTPUT_CHARS``."""
    if len(_dump(payload)) <= _MAX_OUTPUT_CHARS:
        return _dump(payload)
    data = payload.get("data")
    dropped = 0
    if isinstance(data, dict):
        for key in ("lines", "history", "artifacts"):
            seq = data.get(key)
            if isinstance(seq, list):
                while len(_dump(payload)) > _MAX_OUTPUT_CHARS and seq:
                    seq.pop()
                    dropped += 1
    if dropped:
        payload["tool_truncated"] = True
        payload["tool_dropped_entries"] = dropped
    if len(_dump(payload)) <= _MAX_OUTPUT_CHARS:
        return _dump(payload)
    # A single oversized field survived collection-trim — return a compact stub rather than
    # an unbounded (or hard-sliced, invalid-JSON) body.
    return _dump(
        {
            "member": payload.get("member"),
            "kind": payload.get("kind"),
            "ok": payload.get("ok"),
            "tool_truncated": True,
            "detail": "diagnostics payload exceeded the tool output bound",
        }
    )


def _normalize_kind(kind: str) -> str | None:
    k = (kind or "logs").strip().lower()
    if k in ("log", "logs"):
        return "logs"
    if k in ("task", "tasks"):
        return "task"
    return None


async def _run(member: str, kind: str, task_id: str, lines: Any) -> str:
    """The tool body, factored out so tests exercise it without the LangChain wrapper."""
    kind_norm = _normalize_kind(kind)
    if kind_norm is None:
        return _dump(
            _fail("bad_request", f"unknown kind {kind!r}; use 'logs' or 'task'", {"member": (member or "").strip(), "kind": kind})
        )

    target = _resolve(member)
    if not isinstance(target, _Target):
        # A resolution failure (unknown/stopped member) — attach context, no HTTP call made.
        target["member"] = (member or "").strip()
        target["kind"] = kind_norm
        return _dump(target)

    note: str | None = None
    if kind_norm == "logs":
        clamped, note = _clamp_lines(lines)
        result = await _fetch(target, _LOGS_PATH, {"lines": str(clamped)})
    else:
        tid = (task_id or "").strip()
        if not tid:
            return _dump(_fail("bad_request", "task_id is required when kind='task'", {"member": target.member, "kind": "task"}))
        # Quote to a single path segment — a crafted id (``../chat``) cannot escape
        # ``/api/diagnostics/tasks/`` to reach another (mutating) route. Roster-only at the
        # path level too (r2/r6).
        path = _TASK_PATH.format(task_id=quote(tid, safe=""))
        result = await _fetch(target, path, {})

    payload: dict[str, Any] = {"member": target.member, "kind": kind_norm}
    if note:
        payload["note"] = note
    if result.get("ok"):
        # Re-redact at the tool boundary (defense in depth vs an older member), then re-bound.
        from graph.middleware.redaction import redact

        payload["ok"] = True
        payload["data"] = _bound_body(redact(result.get("body")))
    else:
        payload["ok"] = False
        payload["error"] = result.get("error")
        payload["detail"] = result.get("detail")
        for key in ("status", "available"):
            if key in result:
                payload[key] = result[key]
    return _finalize(payload)


def build_fleet_diagnostics_tool() -> list:
    """The guarded ``fleet_diagnostics`` tool, as a one-element list (get_all_tools convention).

    Bound only when ``fleet.diagnostics.enabled`` is set — see ``tools.lg_tools.get_all_tools``.
    """
    from langchain_core.tools import tool

    @tool
    async def fleet_diagnostics(member: str, kind: str = "logs", task_id: str = "", lines: int = _DEFAULT_LINES) -> str:
        """Read a registered fleet member's diagnostics — bounded logs or one exact A2A task.

        Read-only. Use it to troubleshoot a member you MANAGE: where its recent log tail went,
        or the state/history/output of one task it is running. You can only reach members in
        this fleet's roster, addressed by their display name (or id); there is no way to point
        this at an arbitrary host, and it can never start, resume, answer, or otherwise change
        a member.

        Args:
            member: The registered member to inspect, by display name or id.
            kind: ``"logs"`` (default) for the member's recent log tail, or ``"task"`` for one
                A2A task (``task_id`` required).
            task_id: The exact A2A task id — required when ``kind="task"``.
            lines: For ``kind="logs"``, how many recent lines to return (1–1000, default 200).

        Returns a compact JSON object ``{"member", "kind", "ok", ...}`` — ``data`` on success
        (bounded and secret-redacted, preserving the member's own truncated/malformed signals),
        else ``error`` + ``detail`` describing what went wrong (unknown/stopped member,
        unreachable, timeout, unauthorized, no such task, …).
        """
        return await _run(member, kind, task_id, lines)

    return [fleet_diagnostics]
