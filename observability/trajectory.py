"""The trajectory — a per-session append-only log of what the model saw (ADR 0102 S1).

"Model-visible means logged", at the REFERENCE level: each model call's request
envelope is recorded as message ids + content hashes + sizes (plus the stable
system-prefix hash and a bound-tools hash), and every history rewrite —
compaction, pruning, rewind, fork, tool-call repair — lands as a ``surface_op``
event. The BYTES stay where they already live (checkpoint, chat archive, prompt
snapshots); the log is the spine that makes "what did the model see on call N"
answerable, and honest about what pruning has since destroyed (a hash + size
still proves what was sent).

One JSONL per conversation under ``instance_root/trajectory/``, keyed by the
NORMALIZED session id (the ``a2a:`` thread prefix stripped, so middleware
events — keyed by ``state["session_id"]`` — and op events — keyed by thread id —
land in the same file). Size-capped with a single ``.1`` backup, mirroring the
audit log; whole-file retirement rides the thread-retirement path.

Best-effort everywhere: a trajectory failure must never touch a turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Per-file size cap before rotation to a single .1 backup (same posture as the
# audit log): the trajectory is refs-only, so this is months of a busy session.
_MAX_BYTES = 20 * 1024 * 1024

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _normalize(session_or_thread: str) -> str:
    """One file per conversation: the ``a2a:`` thread prefix strips so
    middleware events (session-keyed) and op events (thread-keyed) converge."""
    raw = str(session_or_thread or "unknown")
    if raw.startswith("a2a:"):
        raw = raw[4:]
    return _SAFE.sub("_", raw)[:120] or "unknown"


def sha_text(value) -> str:
    """Stable content hash for a message body. String content hashes as-is;
    block-list content hashes its sorted-key JSON so multimodal/reasoning
    blocks are captured in full, not via a lossy flatten."""
    if isinstance(value, str):
        payload = value
    else:
        try:
            payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = str(value)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def _content_chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


class TrajectoryLog:
    """Append-only per-session JSONL writer (refs, never full text in S1)."""

    def __init__(self, base_dir: str | Path | None = None):
        self._base = Path(base_dir) if base_dir else None
        self._resolved: Path | None = None
        self._lock = threading.Lock()

    def _dir(self) -> Path | None:
        if self._resolved is not None:
            return self._resolved
        try:
            if self._base is None:
                from infra.paths import instance_paths

                candidate = instance_paths().store("trajectory")
            else:
                candidate = self._base
            candidate.mkdir(parents=True, exist_ok=True)
            self._resolved = candidate
            return candidate
        except OSError:
            return None  # degrade to a no-op, like the audit logger

    def path_for(self, session_or_thread: str) -> Path | None:
        d = self._dir()
        return (d / f"{_normalize(session_or_thread)}.jsonl") if d is not None else None

    def append(self, session_or_thread: str, event: dict) -> None:
        """Append one event line. Never raises."""
        try:
            path = self.path_for(session_or_thread)
            if path is None:
                return
            with self._lock:
                try:
                    if path.exists() and path.stat().st_size > _MAX_BYTES:
                        path.replace(path.with_suffix(path.suffix + ".1"))
                except OSError:
                    pass
                entry = {"ts": datetime.now(timezone.utc).isoformat(), **event}
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001 — the trajectory must never break a turn
            log.debug("[trajectory] append failed", exc_info=True)

    def retire(self, session_or_thread: str) -> None:
        """Delete the conversation's log (+ backup) — rides thread retirement,
        so the trajectory outlives checkpoint pruning but not the thread."""
        try:
            path = self.path_for(session_or_thread)
            if path is None:
                return
            for p in (path, path.with_suffix(path.suffix + ".1")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            log.debug("[trajectory] retire failed", exc_info=True)


trajectory_log = TrajectoryLog()


def message_ref(message) -> dict:
    """One message's reference: id + role + content hash + size."""
    content = getattr(message, "content", "")
    role = getattr(message, "type", None) or type(message).__name__.replace("Message", "").lower()
    ref = {
        "id": getattr(message, "id", None),
        "role": str(role),
        "sha": sha_text(content),
        "chars": _content_chars(content),
    }
    if getattr(message, "tool_calls", None):
        ref["tool_calls"] = [tc.get("id") for tc in message.tool_calls if isinstance(tc, dict)]
    return ref


def log_surface_op(
    session_or_thread: str,
    op: str,
    *,
    cause: str = "",
    removed: int = 0,
    removed_ids: list | None = None,
    inserted_ids: list | None = None,
    rewritten_ids: list | None = None,
    kept: int | None = None,
) -> None:
    """Record a history rewrite (ADR 0102 D1) — compact/prune/rewind/fork/repair.

    Counts always; ids when the caller has them cheaply. This is what makes the
    model-visible view at any point DERIVABLE from the log, and every rewrite
    attributable ("who removed that message" has an answer now)."""
    event: dict = {"t": "surface_op", "op": op, "cause": cause, "removed": int(removed)}
    if kept is not None:
        event["kept"] = int(kept)
    if removed_ids:
        event["removed_ids"] = [str(i) for i in removed_ids][:200]
    if inserted_ids:
        event["inserted_ids"] = [str(i) for i in inserted_ids][:200]
    if rewritten_ids:  # bodies replaced IN PLACE (pruning, repair) — position kept, bytes changed
        event["rewritten_ids"] = [str(i) for i in rewritten_ids][:200]
    trajectory_log.append(session_or_thread, event)
