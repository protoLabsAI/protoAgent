"""Audit logging for protoAgent tool executions.

Append-only JSONL of tool-call metadata, enriched with Langfuse trace context for
cross-referencing. The path is **instance-scoped** (ADR 0004) and resolved lazily
so it picks up ``PROTOAGENT_INSTANCE`` (seeded during boot, after this module is
imported). The file **rotates** at a size cap so a busy agent can't fill the disk,
and ``get_recent`` reads only a bounded tail so a large log can't OOM a read.
"""

import json
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Rotate the JSONL when it crosses this; keep one ``.1`` backup. Reads only ever
# touch the live file's tail.
_MAX_BYTES = 50 * 1024 * 1024      # 50 MB
_TAIL_BYTES = 512 * 1024           # get_recent reads at most the last 512 KB
_MAX_SESSIONS = 1000               # cap the in-memory per-session stats dict

_DEFAULT_LEAF = Path("/sandbox") / "audit" / "audit.jsonl"


class AuditLogger:
    """Append-only JSONL audit log for tool executions (rotating, instance-scoped)."""

    def __init__(self, path: str | Path | None = None):
        # Configured base path; the real (instance-scoped, writable) path is
        # resolved on first use so PROTOAGENT_INSTANCE is already seeded.
        self._base = Path(path) if path else _DEFAULT_LEAF
        self._resolved: Path | None = None
        self._session_stats: "OrderedDict[str, dict]" = OrderedDict()

    def _ensure_path(self) -> Path | None:
        """Resolve + create the instance-scoped path, with the standard
        ``/sandbox`` → ``~/.protoagent`` writable fallback. Memoized. ``None`` if
        nothing is writable (audit then degrades to a no-op)."""
        if self._resolved is not None:
            return self._resolved
        from paths import scope_leaf  # ADR 0004 — per-instance scoping (no-op when unset)

        candidate = scope_leaf(self._base)
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            self._resolved = candidate
            return candidate
        except OSError:
            fb = scope_leaf(Path.home() / ".protoagent" / "audit" / candidate.name)
            try:
                fb.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
            self._resolved = fb
            return fb

    @property
    def path(self) -> Path:
        """The resolved on-disk path (for callers/tests that read it directly)."""
        return self._ensure_path() or scope_leaf_safe(self._base)

    def _maybe_rotate(self, path: Path) -> None:
        try:
            if path.exists() and path.stat().st_size > _MAX_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))  # overwrite the single backup
        except OSError:
            pass

    def log(
        self,
        *,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        result_summary: str,
        duration_ms: int,
        success: bool,
    ) -> None:
        trace_id = None
        try:
            import tracing
            trace_id = tracing.current_trace_id() or None
        except Exception:
            pass

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "tool": tool,
            "args": _sanitize_args(args),
            "result_summary": result_summary[:200],
            "duration_ms": duration_ms,
            "success": success,
        }
        if trace_id:
            entry["trace_id"] = trace_id

        path = self._ensure_path()
        if path is not None:
            self._maybe_rotate(path)
            try:
                with path.open("a") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except OSError:
                pass

        stats = self._session_stats.get(session_id)
        if stats is None:
            stats = {"tool_calls": 0, "successes": 0, "failures": 0, "total_ms": 0, "tools_used": set()}
            self._session_stats[session_id] = stats
            # Cap the dict — evict the oldest sessions so a long-lived process
            # serving many sessions doesn't leak memory.
            while len(self._session_stats) > _MAX_SESSIONS:
                self._session_stats.popitem(last=False)
        else:
            self._session_stats.move_to_end(session_id)
        stats["tool_calls"] += 1
        stats["successes" if success else "failures"] += 1
        stats["total_ms"] += duration_ms
        stats["tools_used"].add(tool)

    def get_recent(self, n: int = 20, session_id: str | None = None) -> list[dict[str, Any]]:
        path = self._ensure_path()
        if path is None or not path.exists():
            return []
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - _TAIL_BYTES))
                blob = f.read()
        except OSError:
            return []
        # Drop a possibly-partial first line when we didn't read from the start.
        lines = blob.decode("utf-8", "replace").splitlines()
        if size > _TAIL_BYTES and lines:
            lines = lines[1:]

        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id and entry.get("session_id") != session_id:
                continue
            entries.append(entry)
            if len(entries) >= n:
                break
        entries.reverse()
        return entries

    def get_session_stats(self, session_id: str) -> dict[str, Any]:
        stats = self._session_stats.get(session_id, {})
        if not stats:
            return {"tool_calls": 0}
        return {
            "tool_calls": stats["tool_calls"],
            "successes": stats["successes"],
            "failures": stats["failures"],
            "total_ms": stats["total_ms"],
            "avg_ms": stats["total_ms"] // max(stats["tool_calls"], 1),
            "tools_used": sorted(stats.get("tools_used", set())),
        }


def scope_leaf_safe(p: Path) -> Path:
    """``scope_leaf`` without raising — used only for the ``.path`` fallback."""
    try:
        from paths import scope_leaf
        return scope_leaf(p)
    except Exception:
        return p


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    sanitized = {}
    for k, v in args.items():
        s = str(v)
        sanitized[k] = s[:500] if len(s) > 500 else v
    return sanitized


audit_logger = AuditLogger()
