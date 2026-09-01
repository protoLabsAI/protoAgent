"""Roster-only fleet-member resolution with a bounded log read and an exact task-by-id read
(ADR 0071, #3170 · slices 2–3).

Half of ADR 0071's trust boundary is *addressing*: an operator-authorized managing agent may
inspect a fleet member's recent logs or one exact task, but ONLY a member the configured fleet
roster already knows — NEVER an arbitrary host or URL the model names. This module owns that
boundary and the two reads, and nothing else: it does not (yet) bind an agent tool; the
config-gated binding (``tools.fleet_diagnostics.enabled``, landed default-off in #3304) lands in
a later slice.

Two seams, deliberately NOT reimplemented here:

* **Roster-only addressing (r2).** The target resolves EXCLUSIVELY through
  ``graph.fleet.supervisor.status()`` — the same host + local-peer + remote roster every
  console surface reads. The caller passes a member *name* (or id); a name the roster does not
  contain yields a refusal, and there is no parameter — and no code path — that accepts a host,
  port, or URL. So the model cannot point this at anything the hub does not already own; the
  only address that ever leaves is the member's immutable roster slug.

* **The reads (r1).** Both the recent-log tail and the exact task-by-id view come from the
  member's EXISTING authenticated #3168 endpoints — ``/api/diagnostics/logs`` and
  ``/api/diagnostics/tasks/<task_id>`` — addressed through the hub's own ``/agents/<slug>/*``
  reverse proxy (``operator_api.fleet_routes`` → ``graph.fleet.proxy``). The proxy already owns
  per-slug target resolution (local loopback port / registered remote URL + its bearer), the
  operator→fleet-service-token swap (ADR 0089 D3), and reachability containment (409 not-running
  / 502 unreachable / 504 timeout) — this tool reimplements none of it. It reaches the proxy over
  loopback presenting the fleet service token (accepted as the operator tier, ADR 0089), because
  the import-layering contract forbids ``tools`` importing ``operator_api`` — so HTTP is the
  sanctioned seam, and it is also what makes "read through the real endpoint" literally true.

Containment (r2/r3). A stopped / unreachable / slow member, an unauthorized response, an unknown
member, or a task id the member does not have each becomes a COMPACT structured failure
(``{"ok": False, "error": ...}``) rather than an uncaught exception, so the caller gets an
actionable answer, never a stack trace.

Boundedness + redaction at the DESTINATION (r3/r4). Both reads cross into a model's context, so
each is bounded and secret-redacted at THIS tool's own boundary — the destination — rather than
trusting the member (the source) to have done it. Redaction runs on the full text BEFORE the
tool's own bounding, so truncation can never split a known secret form into the output. The
member already bounds and redacts its response (#3168); this tool re-redacts and re-caps as
defense in depth against an older member that predates #3168 — the log path re-caps the row count
and each line's ``message``; the task path re-caps the history/artifact counts and every text
field — while PRESERVING the server's own ``enabled`` / ``note`` / ``returned`` / ``capacity`` /
``truncated`` / ``malformed`` signals verbatim rather than swallowing them, and signalling any
tool-side trim via ``tool_truncated`` rather than dropping content silently.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

# The member's #3168 endpoints, as the sub-paths the hub proxy forwards
# (``/agents/<slug>/<path>`` — see graph.fleet.proxy). No leading slash: each is joined after
# ``/agents/<slug>/``. The task id is appended (quoted) as the final segment.
_LOGS_SUBPATH = "api/diagnostics/logs"
_TASKS_SUBPATH = "api/diagnostics/tasks"

# Caller-bounded line selector. The member's ring buffer + #3168 endpoint clamp independently
# (operator_api.diagnostics_routes._MAX_LINES); this is the tool-side floor/ceiling so an
# absurd request is normalized before it ever leaves.
_DEFAULT_LINES = 200
_MAX_LINES = 1000

# HTTP timeouts for the loopback hop. The proxy bounds its OWN upstream read (20s → 504); this
# is the near-side belt so a wedged proxy can't park the turn either.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 25.0

# Tool-side compactness: re-cap one line's ``message`` so a pathologically wide log record can't
# blow the model's context. #3168 bounds the SET (line count); this bounds a single field.
_TOOL_TEXT_CAP = 4000

# Task-path bounds. A task's history/artifacts are unbounded in the store and its text fields
# are caller-supplied, so #3168 caps each on the member side — these mirror those caps so the
# tool holds the SAME ceiling even against a member that does not (an older member predating the
# #3168 clamp, or a buggy one, does not get to set this turn's context budget). Same belt as the
# log path's row + field caps, applied to the task shape.
_MAX_TASK_HISTORY = 50
_MAX_TASK_ARTIFACTS = 20
_TASK_TEXT_CAP = 20_000

# How many roster names to name back on an unknown-member miss, so the caller can correct.
_MAX_SUGGESTIONS = 25

# Proxy / member HTTP status → the compact error code the caller sees. Everything the proxy or
# the member's auth can answer with is mapped; anything else falls through to ``http_error``.
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
    "not_found": "diagnostics endpoint not found on this member",
    "not_running": "member is not running",
    "unreachable": "member is not reachable",
    "unavailable": "diagnostics is not available on this member",
    "timeout": "member did not respond in time",
    "request_failed": "member request failed",
    "http_error": "member returned an error",
}


class _Target:
    """A resolved member: its display name + its roster slug. Never carries a model-supplied
    host — the slug is the member's immutable roster id (or the reserved ``host`` slug), and the
    proxy turns it into an actual address."""

    __slots__ = ("member", "slug")

    def __init__(self, member: str, slug: str) -> None:
        self.member = member
        self.slug = slug


def _fail(error: str, detail: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": error, "detail": detail}
    if extra:
        out.update(extra)
    return out


# ── roster resolution (roster-only addressing, r2) ────────────────────────────


def _roster() -> list[dict[str, Any]]:
    """The configured fleet roster (host + local members + remotes). Never raises — a broken
    registry is a diagnostics answer, not a 500."""
    from graph.fleet import supervisor

    try:
        return [e for e in (supervisor.status() or []) if isinstance(e, dict)]
    except Exception:  # noqa: BLE001 — a registry read failure must not crash the tool
        log.exception("[fleet-diagnostics] roster read failed")
        return []


def _display(entry: dict[str, Any]) -> str:
    return str(entry.get("name") or entry.get("id") or "").strip()


def _slug_for(entry: dict[str, Any]) -> str:
    """The proxy slug for a roster entry. The host reaches itself through the reserved ``host``
    slug (graph.fleet.proxy special-cases it); every other member is addressed by its immutable
    id — the SAME slug the console URL uses. Never anything the model supplied."""
    if entry.get("host"):
        return "host"
    return str(entry.get("id") or "").strip()


def _match(roster: list[dict[str, Any]], member: str) -> list[dict[str, Any]]:
    """The roster entries a caller's ``member`` names. Exact id first — an id is unique, so a hit
    there is always a single answer — then EVERY case-insensitive name/label match, because the
    model addresses members by display name and display names are not unique
    (see [[fleet-display-name-two-sources]]: the name has two sources and is operator-editable).

    Returns a list so the caller can tell "no such member" from "which one?" — silently taking
    the first of two same-named members would hand back another agent's logs under the name the
    caller asked for, and nothing in the answer would reveal the substitution.
    """
    want = (member or "").strip()
    if not want:
        return []
    for e in roster:
        if str(e.get("id") or "") == want:
            return [e]
    wl = want.lower()
    return [
        e
        for e in roster
        if str(e.get("name") or "").strip().lower() == wl or str(e.get("label") or "").strip().lower() == wl
    ]


def resolve_member(member: str) -> _Target | dict[str, Any]:
    """Resolve ``member`` to a ``_Target``, or a compact failure dict. Roster-exclusive: an
    unknown member never yields a target, and no host/URL ever comes from the caller."""
    roster = _roster()
    entries = _match(roster, member)
    if not entries:
        names = [n for n in (_display(e) for e in roster) if n][:_MAX_SUGGESTIONS]
        return _fail(
            "unknown_member",
            f"{(member or '').strip()!r} is not a registered fleet member",
            {"available": names},
        )
    if len(entries) > 1:
        # Two members share this display name. Refuse and name the ids rather than picking one:
        # the caller can re-ask by id, which is unique by construction.
        ids = [s for s in (_slug_for(e) for e in entries) if s][:_MAX_SUGGESTIONS]
        return _fail(
            "ambiguous_member",
            f"{(member or '').strip()!r} names {len(entries)} fleet members — re-ask by id",
            {"candidates": ids},
        )
    entry = entries[0]
    slug = _slug_for(entry)
    if not slug:
        # A roster row with neither ``host`` nor an id — unaddressable. Refuse rather than
        # fabricate a target (a diagnostics answer, not a crash).
        return _fail("unknown_member", f"member {_display(entry) or (member or '').strip()!r} has no reachable slug")
    return _Target(_display(entry) or (member or "").strip(), slug)


# ── the member call, through the hub's own /agents/<slug>/* proxy (r1, r3) ─────


def _own_base() -> str | None:
    """The loopback base URL of THIS instance — where the ``/agents/<slug>/*`` proxy lives."""
    from runtime.state import STATE

    port = getattr(STATE, "active_port", None)
    return f"http://127.0.0.1:{port}" if port else None


def _auth_headers() -> dict[str, str]:
    """The operator credential for the loopback call: the fleet service token, which this
    instance's own auth middleware accepts as the operator tier (ADR 0089). The proxy then swaps
    in the per-member credential — this tool never handles a remote's bearer."""
    from graph.fleet.service_token import resolve_service_token

    return {"authorization": f"Bearer {resolve_service_token()}"}


def _make_client():
    """The httpx client for the loopback call. A seam so tests drive a MockTransport without a
    live server + fleet."""
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT))


def _server_detail(resp) -> str | None:
    """The proxy/member's own ``detail`` string on an error response, if any — more actionable
    than a generic message. Read defensively (a non-JSON error body is fine).

    REDACTED before it is capped: this is member-authored content on a path the success-side
    ``redact()`` never sees, and an auth or upstream error is exactly where a member echoes the
    credential it just rejected. Redacting here keeps the module's boundary claim true for the
    failure path too, not only the happy one.
    """
    from graph.middleware.redaction import redact

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return str(redact(body["detail"]))[:_TOOL_TEXT_CAP]
    return None


def _shape(resp) -> dict[str, Any]:
    """A member HTTP response → a normalized result: ``{"ok": True, "body": ...}`` or a compact
    failure. Every non-2xx status is a structured answer, never an exception."""
    status = resp.status_code
    if status >= 400:
        error = _STATUS_ERRORS.get(status, "http_error")
        detail = _server_detail(resp) or _DEFAULT_DETAILS.get(error, f"member returned HTTP {status}")
        return _fail(error, detail, {"status": status})
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON 200 is a malformed member, not a crash
        return {"ok": True, "body": {"malformed": ["response_not_json"], "text": (resp.text or "")[:_TOOL_TEXT_CAP]}}
    return {"ok": True, "body": body}


def _task_subpath(task_id: str) -> str:
    """The task endpoint sub-path for one id. The id is quoted to a single path segment for the
    SAME containment as the slug quote — a hand-edited or odd id can't escape ``/agents/`` to
    another route."""
    return f"{_TASKS_SUBPATH}/{quote(task_id, safe='')}"


async def _get(slug: str, subpath: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """GET one of the member's diagnostics endpoints through the hub proxy, returning a normalized
    result. ``subpath`` is the member-side path under ``/agents/<slug>/`` (the log or task
    subpath, already a safe path). Every network failure mode becomes a compact
    ``{"ok": False, "error": ...}`` — never an exception."""
    import httpx

    base = _own_base()
    if base is None:
        return _fail("unavailable", "diagnostics is unavailable — this instance has no active port")
    # Quote the slug to a single path segment: the slug is a system-owned roster id, but a
    # defensive quote means even a hand-edited id can't escape ``/agents/`` to another route.
    url = f"{base}/agents/{quote(slug, safe='')}/{subpath}"
    client = _make_client()
    try:
        resp = await client.get(url, headers=_auth_headers(), params=params or {})
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return _fail("unreachable", "member is not reachable")
    except httpx.TimeoutException:
        # Covers ReadTimeout/PoolTimeout — the proxy accepted then went silent.
        return _fail("timeout", "member did not respond in time")
    except httpx.HTTPError as exc:
        return _fail("request_failed", f"member request failed: {type(exc).__name__}")
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    return _shape(resp)


# ── output shaping (bounded + redacted, preserved signals, r1/r4) ─────────────


def _clamp_lines(lines: Any) -> tuple[int, str | None]:
    """Coerce a caller's ``lines`` into range, returning ``(value, note)``. Out-of-range is
    clamped, not rejected — a diagnostics read returns the closest useful answer (the same
    clamp-don't-reject contract #3168's endpoint implements)."""
    try:
        value = int(lines)
    except (TypeError, ValueError):
        return _DEFAULT_LINES, f"invalid lines={lines!r}; using {_DEFAULT_LINES}"
    if value < 1:
        return 1, f"lines={value} below minimum; using 1"
    if value > _MAX_LINES:
        return _MAX_LINES, f"lines={value} above maximum; using {_MAX_LINES}"
    return value, None


def _bound_lines(body: Any, limit: int) -> Any:
    """Re-cap the member's already-redacted log body for compactness WITHOUT disturbing its own
    ``enabled`` / ``note`` / ``returned`` / ``capacity`` / ``truncated`` / ``malformed`` signals.
    Marks ``tool_truncated`` only when the tool itself trims something.

    Two independent bounds, because #3168 caps the SET and the FIELD on the member's side and
    this tool must hold even when the member does not — an older member predating #3168's clamp,
    or simply a buggy one, does not get to set this turn's context budget:

    * the ROW COUNT, to the ``limit`` the caller actually asked for (already clamped to
      1–``_MAX_LINES``). The endpoint returns oldest-first, so the TAIL is the recent end —
      keep that, which is what a diagnostics read is for.
    * each row's ``message``, to ``_TOOL_TEXT_CAP``.

    ``returned`` is the member's own count and stays verbatim; a mismatch against ``len(lines)``
    alongside ``tool_truncated`` is the honest signal that this side dropped rows.
    """
    if not isinstance(body, dict):
        return body
    trimmed = False
    lines = body.get("lines")
    if isinstance(lines, list):
        if len(lines) > limit:
            lines = lines[-limit:]
            body["lines"] = lines
            trimmed = True
        for row in lines:
            if isinstance(row, dict) and isinstance(row.get("message"), str) and len(row["message"]) > _TOOL_TEXT_CAP:
                row["message"] = row["message"][:_TOOL_TEXT_CAP]
                trimmed = True
    if trimmed:
        body["tool_truncated"] = True
    return body


def _bound_task(body: Any) -> Any:
    """Re-cap the member's already-redacted task view for compactness WITHOUT disturbing its own
    ``state`` / ``truncated`` / ``malformed`` / ``last_updated`` signals. Marks ``tool_truncated``
    only when the tool itself trims something.

    The same belt as ``_bound_lines``, applied to the task shape, because #3168 bounds the
    history/artifact COUNTS and every text FIELD on the member's side and this tool must hold even
    when the member does not:

    * ``history`` to ``_MAX_TASK_HISTORY`` rows, keeping the recent TAIL — the endpoint returns
      history oldest-first, so the tail is the recent end a diagnostics read is for;
    * ``artifacts`` to ``_MAX_TASK_ARTIFACTS`` rows, keeping the HEAD (the member returns
      ``artifacts[:_MAX_ARTIFACTS]``, so the head is what it advertises);
    * every text field — top-level ``status_message`` / ``accumulated_text`` and each history /
      artifact entry's ``text`` — to ``_TASK_TEXT_CAP``.

    The member's own ``truncated`` list stays verbatim; a fresh ``tool_truncated`` alongside it is
    the honest signal that THIS side dropped something rather than the member.
    """
    if not isinstance(body, dict):
        return body
    trimmed = False

    def _cap(value: Any) -> Any:
        nonlocal trimmed
        if isinstance(value, str) and len(value) > _TASK_TEXT_CAP:
            trimmed = True
            return value[:_TASK_TEXT_CAP]
        return value

    for field in ("status_message", "accumulated_text"):
        if field in body:
            body[field] = _cap(body.get(field))

    history = body.get("history")
    if isinstance(history, list):
        if len(history) > _MAX_TASK_HISTORY:
            history = history[-_MAX_TASK_HISTORY:]
            body["history"] = history
            trimmed = True
        for entry in history:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                entry["text"] = _cap(entry["text"])

    artifacts = body.get("artifacts")
    if isinstance(artifacts, list):
        if len(artifacts) > _MAX_TASK_ARTIFACTS:
            artifacts = artifacts[:_MAX_TASK_ARTIFACTS]
            body["artifacts"] = artifacts
            trimmed = True
        for entry in artifacts:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                entry["text"] = _cap(entry["text"])

    if trimmed:
        body["tool_truncated"] = True
    return body


async def read_member_logs(member: str, lines: Any = _DEFAULT_LINES) -> dict[str, Any]:
    """Read a registered fleet member's bounded recent logs through the #3168 endpoint.

    Roster-only: ``member`` is a display name or id that MUST be in the fleet roster; anything
    else is refused with no HTTP call made. ``lines`` is the caller-bounded selector (clamped to
    1–1000). Returns a compact dict — ``{"member", "slug", "ok": True, "logs", ["note"]}`` on
    success (bounded + secret-redacted, preserving the member's own signals), else
    ``{"member", "ok": False, "error", "detail", ...}`` describing the failure.

    Read-only, and not yet exposed to the model — the config-gated tool binding lands in a later
    slice; this is the resolution + log-read core it will wrap.
    """
    target = resolve_member(member)
    if not isinstance(target, _Target):
        # A resolution failure (unknown member) — attach the input for context; no HTTP made.
        target["member"] = (member or "").strip()
        return target

    clamped, note = _clamp_lines(lines)
    result = await _get(target.slug, _LOGS_SUBPATH, {"lines": str(clamped)})

    payload: dict[str, Any] = {"member": target.member, "slug": target.slug}
    if note:
        payload["note"] = note
    if result.get("ok"):
        # Re-redact at the tool boundary (defense in depth vs an older member), then re-bound —
        # the member's own truncated/malformed/enabled/note signals pass through untouched.
        from graph.middleware.redaction import redact

        payload["ok"] = True
        payload["logs"] = _bound_lines(redact(result.get("body")), clamped)
    else:
        payload["ok"] = False
        payload["error"] = result.get("error")
        payload["detail"] = result.get("detail")
        for key in ("status", "available"):
            if key in result:
                payload[key] = result[key]
    return payload


async def read_member_task(member: str, task_id: str) -> dict[str, Any]:
    """Read one exact A2A task by id from a registered fleet member through the #3168
    ``/agents/<slug>/api/diagnostics/tasks/<task_id>`` endpoint.

    Roster-only, exactly like :func:`read_member_logs`: ``member`` is a display name or id that
    MUST be in the fleet roster; anything else is refused with no HTTP call made, and there is no
    host/URL parameter. ``task_id`` addresses ONE exact task — a task the member does not have is
    a compact ``{"error": "not_found"}`` failure, never an exception. Returns a compact dict —
    ``{"member", "slug", "task_id", "ok": True, "task"}`` on success (bounded + secret-redacted at
    this tool's boundary, preserving the member's own ``truncated`` / ``malformed`` signals), else
    ``{"member", "task_id", "ok": False, "error", "detail", ...}`` describing the failure.

    Read-only, and not yet exposed to the model — the config-gated tool binding lands in a later
    slice; this is the resolution + exact-task-read core it will wrap.
    """
    tid = (task_id or "").strip()
    if not tid:
        # No id at all — a resolution-level refusal; no HTTP made and no member needed.
        return _fail("invalid_task_id", "a task id is required", {"member": (member or "").strip(), "task_id": ""})

    target = resolve_member(member)
    if not isinstance(target, _Target):
        # A resolution failure (unknown member) — attach the inputs for context; no HTTP made.
        target["member"] = (member or "").strip()
        target["task_id"] = tid
        return target

    result = await _get(target.slug, _task_subpath(tid))

    payload: dict[str, Any] = {"member": target.member, "slug": target.slug, "task_id": tid}
    if result.get("ok"):
        # Redact at the tool boundary (the DESTINATION) BEFORE bounding, so truncation can never
        # split a known secret form into the output; then re-bound. The member's own
        # truncated/malformed/state signals pass through untouched.
        from graph.middleware.redaction import redact

        payload["ok"] = True
        payload["task"] = _bound_task(redact(result.get("body")))
    else:
        payload["ok"] = False
        payload["error"] = result.get("error")
        payload["detail"] = result.get("detail")
        for key in ("status", "available"):
            if key in result:
                payload[key] = result[key]
    return payload
