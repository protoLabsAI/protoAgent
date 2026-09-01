"""Guarded read-only fleet diagnostics tool (#3170, ADR 0071).

Lets an *explicitly authorized* managing agent inspect ONE registered fleet member's
bounded log snapshot or one exact A2A task by id — and nothing else. The trust boundary
(ADR 0071) is built from three independent guarantees, each of which is exercised by a
test in ``tests/test_fleet_diagnostics_tool.py``:

* **Roster-only addressing.** The member is resolved EXCLUSIVELY through the configured
  fleet roster (``graph.fleet.supervisor.status`` — the host, local workspaces, and
  registered remotes). A name/id that isn't on the roster is refused; the caller cannot
  point the tool at an arbitrary host or URL, and the tool never constructs an alternate
  addressing path of its own.
* **Reuse of the #3168 API.** The member is reached ONLY through the existing
  operator-authenticated ``/agents/<slug>/api/diagnostics/*`` proxy — the same bounded,
  redacted, read-only endpoints the console drawer uses. There is no second task/log read
  path. The call authenticates with the loopback-only fleet service token (ADR 0089),
  which the hub accepts as the operator tier and swaps for a local member.
* **Default-off model exposure.** The tool binds to the model ONLY when
  ``fleet.diagnostics.enabled`` is set (``LangGraphConfig.fleet_diagnostics_enabled``,
  default ``False``), so an ordinary agent never sees it — the gate lives in
  ``tools.lg_tools.get_all_tools``.

It is **read-only** by construction: it issues GET requests to the diagnostics endpoints
and nothing else. It cannot start a member, resume/answer a task or HITL prompt, mutate a
checkpoint, or change configuration. Output is bounded, preserves the server's own
truncation/malformed signals, and is passed through the shared credential redactor before
it returns (defence in depth over the member's own redaction, so a member on older code
is still scrubbed at this boundary).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx
from langchain_core.tools import tool

log = logging.getLogger("protoagent.tools.fleet_diagnostics")

# Line-selector bounds. Mirror ``operator_api.diagnostics_routes`` so the caller-facing
# contract matches the server's; the server clamps again, this just avoids sending nonsense.
_DEFAULT_LINES = 200
_MAX_LINES = 1000
# The proxy's view/API read lane is 20s (``graph.fleet.proxy._READ_TIMEOUT``); match it so a
# member that accepts-then-stalls surfaces as the proxy's 504, not our own transport error.
_HTTP_TIMEOUT = 20.0
# A final belt on total tool output size, independent of (and stricter than) the server's
# per-field caps — a diagnostics read must never blow the LLM context budget.
_MAX_OUTPUT_CHARS = 20_000


def _compact(payload: dict[str, Any]) -> str:
    """Serialize a result dict as compact JSON, stringifying anything odd rather than raising."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _fail(member: str, detail: str, **extra: Any) -> str:
    """A compact, actionable failure — never an uncaught exception (r5)."""
    return _compact({"ok": False, "member": member, "error": detail, **extra})


def _resolve_member(member: str) -> tuple[dict | None, str | None]:
    """Resolve a member name/id to its roster entry, or ``(None, reason)``.

    Roster-ONLY (ADR 0071): the host + local workspaces + registered remotes exactly as
    ``supervisor.status()`` reports them. A name/id not on the roster is refused, so the
    caller can never point the tool at a host/URL the roster doesn't already know.
    """
    from graph.fleet import supervisor

    wanted = (member or "").strip()
    if not wanted:
        return None, "member is required — pass a fleet member's id or display name"
    try:
        roster = supervisor.status()
    except Exception as exc:  # noqa: BLE001 — a roster read failure is an answer, not a crash
        log.warning("[fleet-diagnostics] roster read failed: %s", exc)
        return None, "could not read the fleet roster"
    by_id = {str(e.get("id")): e for e in roster if e.get("id")}
    if wanted in by_id:
        return by_id[wanted], None
    matches = [e for e in roster if str(e.get("name", "")).strip().lower() == wanted.lower()]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"member {wanted!r} is ambiguous — pass its exact id instead of the display name"
    known = ", ".join(sorted(str(e.get("name") or e.get("id")) for e in roster)) or "(none)"
    return None, f"no fleet member {wanted!r} on the roster — known members: {known}"


def _clamp_lines(lines: Any) -> int:
    """Coerce the caller's ``lines`` into ``[1, _MAX_LINES]`` (the server clamps again)."""
    try:
        value = int(lines)
    except (TypeError, ValueError):
        return _DEFAULT_LINES
    return max(1, min(value, _MAX_LINES))


def _detail_of(resp: httpx.Response) -> str:
    """The server's own ``detail`` for a non-200, or a bounded slice of the body."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except ValueError:
        pass
    return (resp.text or "").strip()[:200]


def _bound_output(out: dict[str, Any]) -> str:
    """Serialize ``out``, trimming the heaviest collections so the tool NEVER exceeds
    ``_MAX_OUTPUT_CHARS``. Marks ``output_truncated`` when the tool trimmed further — kept
    DISTINCT from the server's own ``truncated``/``note`` signals, which are preserved."""
    text = _compact(out)
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    trimmed = dict(out)
    trimmed["output_truncated"] = True
    # Logs: keep the most recent lines, dropping from the front (oldest) until it fits.
    lines = trimmed.get("lines")
    if isinstance(lines, list):
        while lines and len(_compact(trimmed)) > _MAX_OUTPUT_CHARS:
            lines = lines[len(lines) // 4 + 1 :]
            trimmed["lines"] = lines
            trimmed["returned"] = len(lines)
    # Task: shed the unbounded-in-store collections/text fields in order of weight.
    task = trimmed.get("task")
    if isinstance(task, dict):
        for key in ("history", "artifacts", "accumulated_text", "status_message"):
            if len(_compact(trimmed)) <= _MAX_OUTPUT_CHARS:
                break
            if key in task:
                task = {**task, key: ([] if isinstance(task.get(key), list) else "")}
                trimmed["task"] = task
    text = _compact(trimmed)
    if len(text) > _MAX_OUTPUT_CHARS:
        # Last resort — a bound we cannot exceed even if a single field is still oversized.
        text = _compact(
            {
                "ok": True,
                "member": out.get("member"),
                "member_id": out.get("member_id"),
                "kind": out.get("kind"),
                "output_truncated": True,
                "note": "diagnostics output exceeded the tool's output bound and was dropped",
            }
        )
    return text


async def _http_get(url: str, params: dict | None, headers: dict) -> httpx.Response:
    """One bounded loopback GET (its own seam so tests can stub the transport)."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        return await client.get(url, params=params, headers=headers)


@tool
async def fleet_diagnostics(member: str, what: str = "logs", task_id: str = "", lines: int = 200) -> str:
    """Inspect a registered fleet member's recent logs or one exact A2A task — READ-ONLY.

    Resolves ``member`` through the fleet roster (a member id or display name) and reads it
    via the operator-authenticated diagnostics API. It can ONLY read; it cannot start a
    member, resume/answer a task or human prompt, edit checkpoints, or change any config.

    Args:
        member: The fleet member's id or display name, as it appears on the roster. A
            name/id not on the roster is refused — you cannot target an arbitrary host.
        what: ``"logs"`` (default) for a bounded tail of the member's log ring, or
            ``"task"`` to inspect one exact A2A task (requires ``task_id``).
        task_id: The exact A2A task id to inspect. Required when ``what="task"``.
        lines: For ``what="logs"``, how many of the most recent log lines to return
            (1–1000, default 200). Ignored for tasks.

    Returns a compact JSON object: for logs, ``{ok, member, kind, enabled, capacity,
    returned, lines, note?}``; for a task, ``{ok, member, kind, task}`` with the task's
    state, history, artifacts, accumulated output, and the server's truncated/malformed
    signals preserved. On any failure it returns ``{ok: false, member, error, ...}`` rather
    than raising.
    """
    entry, reason = _resolve_member(member)
    if entry is None:
        return _fail(member, reason or "member could not be resolved")
    slug = str(entry.get("id"))
    display = str(entry.get("name") or slug)

    kind = (what or "logs").strip().lower()
    if kind in ("log", "logs"):
        kind = "logs"
    elif kind in ("task", "tasks"):
        kind = "task"
    else:
        return _fail(display, f"unknown 'what' {what!r} — use 'logs' or 'task'", member_id=slug)

    from runtime.state import STATE

    port = getattr(STATE, "active_port", None)
    if not port:
        return _fail(display, "no active server port on this instance — cannot reach the diagnostics proxy", member_id=slug)

    try:
        from graph.fleet.service_token import resolve_service_token

        token = resolve_service_token()
    except Exception as exc:  # noqa: BLE001 — no fleet token ⇒ we cannot authenticate the read
        log.warning("[fleet-diagnostics] could not resolve the fleet service token: %s", exc)
        return _fail(display, "could not resolve the fleet service token to authenticate the read", member_id=slug)

    base = f"http://127.0.0.1:{port}/agents/{quote(slug, safe='')}/api/diagnostics"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if kind == "logs":
        url, params = f"{base}/logs", {"lines": _clamp_lines(lines)}
    else:
        tid = (task_id or "").strip()
        if not tid:
            return _fail(display, "task_id is required when what='task'", member_id=slug)
        url, params = f"{base}/tasks/{quote(tid, safe='')}", None

    try:
        resp = await _http_get(url, params, headers)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return _fail(display, "the local diagnostics proxy was unreachable", member_id=slug)
    except httpx.TimeoutException:
        return _fail(display, "the diagnostics read timed out", member_id=slug)
    except httpx.HTTPError as exc:
        return _fail(display, f"diagnostics transport error: {str(exc)[:160]}", member_id=slug)

    if resp.status_code != 200:
        # The proxy/member already contain the failure — surface it as a compact, actionable
        # message rather than an exception (r5). 409 = member not running, 502 = unreachable,
        # 504 = member didn't answer in time, 401/403 = auth, 404 = no such task, 503 = no store.
        return _fail(
            display,
            f"diagnostics read failed (HTTP {resp.status_code}): {_detail_of(resp) or 'no detail'}",
            member_id=slug,
            status=resp.status_code,
        )

    try:
        payload = resp.json()
    except ValueError:
        return _fail(display, "member returned a non-JSON diagnostics body", member_id=slug)

    from graph.middleware.redaction import redact

    if kind == "logs":
        raw_lines = payload.get("lines") if isinstance(payload, dict) else None
        raw_lines = raw_lines if isinstance(raw_lines, list) else []
        cap = _clamp_lines(lines)
        if len(raw_lines) > cap:  # honor the caller's bound even if the member returned more
            raw_lines = raw_lines[-cap:]
        out: dict[str, Any] = {
            "ok": True,
            "member": display,
            "member_id": slug,
            "kind": "logs",
            "enabled": payload.get("enabled") if isinstance(payload, dict) else None,
            "capacity": payload.get("capacity") if isinstance(payload, dict) else None,
            "returned": len(raw_lines),
            "lines": raw_lines,
        }
        note = payload.get("note") if isinstance(payload, dict) else None
        if note:
            out["note"] = note
    else:
        out = {"ok": True, "member": display, "member_id": slug, "kind": "task", "task": payload}

    # Redact at THIS boundary too (belt over the member's own redaction), then bound.
    return _bound_output(redact(out))
