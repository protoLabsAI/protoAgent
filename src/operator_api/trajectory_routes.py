"""Trajectory read surface (ADR 0102 D4/S2, #2806) — events + reconstruction.

Read-only routes over the per-conversation trajectory log the S1 writer
produces (``observability/trajectory.py``):

- ``GET /api/trajectory/{session_id}`` — the event stream (tail-biased paging).
- ``GET /api/trajectory/{session_id}/call/{n}`` — the reconstructed request
  envelope for the nth ``request`` event, with per-message availability joined
  against the LIVE checkpoint: ``available`` (bytes present and hash-identical,
  preview included) · ``rewritten`` (same id, different bytes — a pruner stub or
  repair replaced the body in place) · ``missing`` (compacted/rewound away; the
  chat archive may hold the text, resolvable via memory recall — automatic
  archive resolution is a later slice, and this route says so rather than
  pretending).

Honest by construction: the log holds hashes + sizes, so even a ``missing``
message is still *proven* — what was sent, how big, in what position.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("protoagent.server")

# Per-message preview cap in a reconstruction — the viewer wants orientation,
# not a second transcript dump; the full text lives in the checkpoint/export.
_PREVIEW_CHARS = 2_000


def _read_events(session_id: str) -> list[dict] | None:
    """All events for the conversation, or ``None`` when no log exists."""
    from observability.trajectory import trajectory_log

    path = trajectory_log.path_for(session_id)
    if path is None or not path.exists():
        return None
    events: list[dict] = []
    # Include the rotated backup's tail so a rotation boundary doesn't amputate
    # the visible history mid-conversation.
    backup = path.with_suffix(path.suffix + ".1")
    for p in (backup, path):
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn tail line from a crash mid-append
        except OSError:
            continue
    return events


async def _thread_messages(session_id: str) -> dict[str, object]:
    """id → message for the session's LIVE thread (empty when unavailable)."""
    from runtime.state import STATE

    if STATE.graph is None:
        return {}
    try:
        snapshot = await STATE.graph.aget_state({"configurable": {"thread_id": f"a2a:{session_id}"}})
        messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])
        return {getattr(m, "id", None): m for m in messages if getattr(m, "id", None)}
    except Exception:  # noqa: BLE001 — a read surface never 500s over a checkpoint hiccup
        log.debug("[trajectory] thread read failed for %s", session_id, exc_info=True)
        return {}


def _preview(content) -> str:
    if isinstance(content, str):
        return content[:_PREVIEW_CHARS]
    try:
        return json.dumps(content, ensure_ascii=False, default=str)[:_PREVIEW_CHARS]
    except (TypeError, ValueError):
        return str(content)[:_PREVIEW_CHARS]


def register_trajectory_routes(app) -> None:
    """Wire the read-only /api/trajectory/* surface (ADR 0102 S2)."""

    @app.get("/api/trajectory/{session_id}")
    async def _api_trajectory_events(session_id: str, limit: int = 200, before: int | None = None):
        """The conversation's event stream, newest-biased: the LAST ``limit``
        events by default; ``before=<index>`` pages backward (events keep their
        absolute ``index`` so paging is stable across appends)."""
        events = _read_events(session_id)
        if events is None:
            return {"found": False, "events": [], "total": 0}
        indexed = [{"index": i, **e} for i, e in enumerate(events)]
        window = [e for e in indexed if before is None or e["index"] < before]
        limit = max(1, min(int(limit), 1000))
        return {"found": True, "events": window[-limit:], "total": len(events)}

    @app.get("/api/trajectory/{session_id}/call/{n}")
    async def _api_trajectory_call(session_id: str, n: int):
        """Reconstruct the nth model call's request envelope (0-indexed;
        ``-1`` = the latest). Availability is judged against the live
        checkpoint by id + content hash — the join the S1 refs exist for."""
        from observability.trajectory import sha_text

        events = _read_events(session_id)
        if events is None:
            return {"found": False, "reason": "no_trajectory"}
        requests = [e for e in events if e.get("t") == "request"]
        if not requests:
            return {"found": False, "reason": "no_requests"}
        try:
            req = requests[n]
        except IndexError:
            return {"found": False, "reason": "out_of_range", "calls": len(requests)}

        live = await _thread_messages(session_id)
        messages = []
        counts = {"available": 0, "rewritten": 0, "missing": 0}
        for ref in req.get("msgs") or []:
            mid = ref.get("id")
            entry = dict(ref)
            m = live.get(mid) if mid else None
            if m is None:
                entry["status"] = "missing"  # compacted/rewound away; the chat archive may hold it
            elif sha_text(getattr(m, "content", "")) == ref.get("sha"):
                entry["status"] = "available"
                entry["preview"] = _preview(getattr(m, "content", ""))
            else:
                entry["status"] = "rewritten"  # same id, new bytes — pruner stub / repair
                entry["preview"] = _preview(getattr(m, "content", ""))
            counts[entry["status"]] += 1
            messages.append(entry)
        return {
            "found": True,
            "call": n if n >= 0 else len(requests) + n,
            "calls": len(requests),
            "model": req.get("model", ""),
            "stable_sha": req.get("stable_sha", ""),
            "tools_sha": req.get("tools_sha", ""),
            "tools_count": req.get("tools_count", 0),
            "ts": req.get("ts", ""),
            "messages": messages,
            "availability": counts,
        }
