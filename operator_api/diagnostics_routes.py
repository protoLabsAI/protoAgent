"""Member-local diagnostics reads — bounded logs and exact A2A task inspection (#3168).

Built-in infrastructure rather than a plugin, because **every** member must serve the
same contract: the console drawer (#3169) and the guarded PM tool (#3170) reach a local
peer and a registered remote member through the one ``/agents/{slug}/*`` proxy, so a
surface only some members expose would be useless to both.

Security posture. Logs and task rows carry prompts, user content, tool arguments, and
credentials, so this is sensitive **operator** data:

* Mounted under ``/api`` and deliberately absent from ``a2a_impl.auth._PUBLIC_PREFIXES``.
  That middleware is default-deny, and its federation tier is confined to ``/a2a`` +
  ``/v1`` and denied ``/api`` outright — so operator-tier gating and federation exclusion
  come from the mount point alone. The guarantee is POSITIONAL, which is why
  ``tests/test_diagnostics_routes.py`` asserts a federation bearer is rejected rather
  than trusting the prefix to stay put.
* Read-only. Nothing here starts a member, resumes a task, answers a HITL prompt, or
  writes config.
* Every response goes through ``graph.middleware.redaction.redact`` before it leaves.

Failure containment. A member that is stopped, unreachable, or slow is the **proxy's**
case and already resolved there (``graph/fleet/proxy.py`` → 409 / 502 / 504, never a
500). What is left to this module is its own local failure modes — a nonsense ``lines``,
an unknown task id, and a malformed store row — each of which returns a structured
non-500 body.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

# Read caps. These bound the RESPONSE; the ring buffer separately bounds what is
# retained at all, so a caller cannot ask for more history than exists.
_DEFAULT_LINES = 200
_MAX_LINES = 1000
# A task's history/artifacts are unbounded in the store — a long session accumulates
# both without limit — so the reader truncates and SAYS it truncated. Silent trimming
# would let an operator draw conclusions from a partial history.
_MAX_HISTORY = 50
_MAX_ARTIFACTS = 20
_MAX_TEXT_CHARS = 20_000


def _clamp_lines(lines: Any) -> tuple[int, str | None]:
    """Coerce a caller's ``lines`` into range. Returns ``(value, note)``.

    Out-of-range is clamped rather than rejected — a diagnostics read should return
    the closest useful answer, not a 422, when someone types ``lines=99999``. The note
    tells the caller their input was adjusted so a UI can say so.
    """
    if lines is None:
        return _DEFAULT_LINES, None
    try:
        value = int(lines)
    except (TypeError, ValueError):
        return _DEFAULT_LINES, f"invalid lines={lines!r}; using {_DEFAULT_LINES}"
    if value < 1:
        return 1, f"lines={value} below minimum; using 1"
    if value > _MAX_LINES:
        return _MAX_LINES, f"lines={value} above maximum; using {_MAX_LINES}"
    return value, None


def _truncate(text: str, limit: int | None = None) -> tuple[str, bool]:
    """Cap ``text``, reporting whether it was cut.

    ``limit`` resolves at CALL time, not as a default argument — binding the module
    constant in the signature would freeze the cap at import and make it unoverridable.
    """
    if limit is None:
        limit = _MAX_TEXT_CHARS
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _text_from_parts(parts: Any) -> str:
    """Concatenate the text parts of an A2A message/artifact.

    Total defensive read: a ``parts`` blob comes out of a JSON column and may be any
    shape at all (a malformed row, a schema change, a hand-edited DB). Anything that
    isn't a text part is skipped rather than raising.
    """
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("kind") == "text" and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "".join(out)


def _summarize_task(row: Any) -> dict[str, Any]:
    """Shape one task row into the diagnostics view, bounded and non-raising.

    Malformed columns degrade to a ``malformed`` list on the response instead of a 500 —
    the operator reading this is usually here BECAUSE something is broken, so a partial
    answer that names what it couldn't parse beats an error page.
    """
    malformed: list[str] = []
    truncated: list[str] = []

    def _json_field(name: str, default: Any) -> Any:
        value = getattr(row, name, None)
        if value is None:
            return default
        if not isinstance(value, type(default)):
            malformed.append(name)
            return default
        return value

    status = _json_field("status", {})
    history = _json_field("history", [])
    artifacts = _json_field("artifacts", [])

    state = status.get("state") if isinstance(status, dict) else None
    status_message = ""
    if isinstance(status, dict) and isinstance(status.get("message"), dict):
        status_message = _text_from_parts(status["message"].get("parts"))

    # Accumulated output = the text the member has produced on this task, which is what
    # an operator chasing "where did the answer go" actually wants.
    accumulated = "".join(_text_from_parts(a.get("parts")) for a in artifacts if isinstance(a, dict))
    accumulated, hit = _truncate(accumulated)
    if hit:
        truncated.append("accumulated_text")

    if len(history) > _MAX_HISTORY:
        history = history[-_MAX_HISTORY:]
        truncated.append("history")
    if len(artifacts) > _MAX_ARTIFACTS:
        artifacts = artifacts[:_MAX_ARTIFACTS]
        truncated.append("artifacts")

    trimmed_history = []
    for msg in history:
        if not isinstance(msg, dict):
            malformed.append("history_entry")
            continue
        text, hit = _truncate(_text_from_parts(msg.get("parts")))
        if hit and "history" not in truncated:
            truncated.append("history")
        trimmed_history.append(
            {
                "role": msg.get("role"),
                "message_id": msg.get("messageId"),
                "text": text,
            }
        )

    trimmed_artifacts = []
    for art in artifacts:
        if not isinstance(art, dict):
            malformed.append("artifact_entry")
            continue
        text, hit = _truncate(_text_from_parts(art.get("parts")))
        if hit and "artifacts" not in truncated:
            truncated.append("artifacts")
        trimmed_artifacts.append(
            {
                "artifact_id": art.get("artifactId"),
                "name": art.get("name"),
                "text": text,
            }
        )

    last_updated = getattr(row, "last_updated", None)

    return {
        "task_id": getattr(row, "id", None),
        "context_id": getattr(row, "context_id", None),
        "state": state,
        "status_message": status_message,
        "last_updated": last_updated.isoformat() if hasattr(last_updated, "isoformat") else last_updated,
        "history": trimmed_history,
        "artifacts": trimmed_artifacts,
        "accumulated_text": accumulated,
        "truncated": sorted(set(truncated)),
        "malformed": sorted(set(malformed)),
    }


def register_diagnostics_routes(app) -> None:
    """Register the read-only ``/api/diagnostics/*`` routes on ``app``."""

    @app.get("/api/diagnostics/logs")
    async def _api_diagnostics_logs(lines: str | None = None):
        """The tail of this member's in-process log ring (#3168).

        ``lines`` is typed ``str`` ON PURPOSE — do not "fix" it to ``int``. FastAPI
        would then reject ``lines=abc`` with a 422 before this handler ran, which
        contradicts the clamp-don't-reject contract ``_clamp_lines`` implements: a
        diagnostics read returns the closest useful answer rather than an error page.

        The source is deliberately NOT ``agent.log``: that file is a raw stdout/stderr redirect the
        fleet supervisor sets up for hub-spawned local children only, so it does not
        exist for a remote, foreground, or desktop-run member. The ring is in-process
        and therefore uniform across every deployment shape.
        """
        from graph.middleware.redaction import redact
        from observability.logging_config import log_buffer

        limit, note = _clamp_lines(lines)
        buffer = log_buffer()
        if buffer is None:
            # configure_logging() never ran — a library/test embedding, not a server.
            return {"enabled": False, "lines": [], "returned": 0, "capacity": 0, "note": "log buffer not configured"}

        records = buffer.snapshot(limit)
        payload = {
            "enabled": True,
            "capacity": buffer.capacity(),
            "returned": len(records),
            # Redact at READ time on the already-bounded slice. Scrubbing on the
            # logging hot path would tax every log call in the process to serve a
            # read that almost never happens.
            "lines": redact(records),
        }
        if note:
            payload["note"] = note
        return payload

    @app.get("/api/diagnostics/tasks/{task_id}")
    async def _api_diagnostics_task(task_id: str):
        """Inspect one exact A2A task by id — state, history, artifacts, output."""
        from graph.middleware.redaction import redact
        from runtime.state import STATE

        engine = getattr(STATE, "a2a_task_engine", None)
        if engine is None:
            return JSONResponse(
                {"detail": "task store is not configured on this member", "task_id": task_id},
                status_code=503,
            )

        try:
            from a2a.server.tasks.database_task_store import TaskModel
            from sqlalchemy import select

            async with engine.connect() as conn:
                row = (await conn.execute(select(TaskModel).where(TaskModel.id == task_id))).mappings().first()
        except Exception:  # noqa: BLE001 — a store read failure is a diagnostics answer, not a 500
            log.exception("[diagnostics] task read failed for %s", task_id)
            return JSONResponse(
                {"detail": "task store read failed", "task_id": task_id},
                status_code=503,
            )

        if row is None:
            return JSONResponse(
                {"detail": "no such task on this member", "task_id": task_id},
                status_code=404,
            )

        # ``.mappings()`` gives a dict-like row; _summarize_task reads by attribute so
        # it works against both that and an ORM object.
        return redact(_summarize_task(_Row(dict(row))))


class _Row:
    """Attribute view over a mapping row, so the summarizer is storage-shape agnostic."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.__dict__.update(data)
