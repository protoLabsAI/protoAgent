"""Audit logging for protoAgent tool executions.

Writes JSONL entries to /sandbox/audit/audit.jsonl with tool call metadata.
Enhanced with Langfuse trace context for cross-referencing.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    """Append-only JSONL audit log for tool executions."""

    def __init__(self, path: str | Path = "/sandbox/audit/audit.jsonl"):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # /sandbox isn't writable (non-root CI runner, local `python
            # server.py`, etc.) — fall back to a per-user path so audit
            # logging degrades gracefully instead of crashing at import.
            self.path = Path.home() / ".protoagent" / "audit" / self.path.name
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        self._session_stats: dict[str, dict] = {}

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

        try:
            with self.path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

        stats = self._session_stats.setdefault(session_id, {
            "tool_calls": 0, "successes": 0, "failures": 0, "total_ms": 0,
            "tools_used": set(),
        })
        stats["tool_calls"] += 1
        stats["successes" if success else "failures"] += 1
        stats["total_ms"] += duration_ms
        stats["tools_used"].add(tool)

    def get_recent(
        self, n: int = 20, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text().strip().splitlines()
        except OSError:
            return []

        entries = []
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


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    sanitized = {}
    for k, v in args.items():
        s = str(v)
        sanitized[k] = s[:500] if len(s) > 500 else v
    return sanitized


audit_logger = AuditLogger()
