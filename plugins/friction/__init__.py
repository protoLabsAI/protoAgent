"""Friction Log — the agent records its own rough edges; the backlog writes itself.

A self-report pattern in the spirit of NousResearch/hermes-agent: a self-improving
agent captures where it hit friction and feeds that back into improving its harness
(tools/framework) and its model (training signal). Two capture channels, one ledger:

  * AGENT-INITIATED — `record_friction`: the agent flags what a detector can't see —
    a missing or awkward tool, a confusing error, reaching for a general escape hatch
    (a shell tool) for something that should be first-class, a wrong path it recognizes.
    High-signal: the model knows when it's frustrated. (Prompt it to use this in your
    agent's persona/system prompt — the tool exists, but the model has to reach for it.)
  * AUTO-CAPTURE — `FrictionMiddleware.wrap_tool_call`: escape-hatch reaches (a shell/exec
    tool being invoked = a missing-tool signal, logged with the command) and genuine tool
    errors, with no agent effort. HITL/interrupt control-flow is filtered out (a tool
    pausing for approval or delegating is not friction).

`kind` splits the backlog: `"harness"` → an improvement to the tools/framework;
`"model"` → a labeled trace worth learning from. `friction_review` surfaces it;
`resolve_friction` dismisses entries once fixed (a `resolved_at` stamp in place — the
ledger stays append-only, so the audit trail survives). Enable via
`plugins: { enabled: [friction] }`. The ledger path is `$FRICTION_LOG` or, by
default, `<instance data dir>/friction/friction.jsonl`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

_KINDS = ("harness", "model")
_SEVERITIES = ("minor", "major")
_SEVERITY_RANK = {"minor": 1, "major": 2}

# A general shell/exec tool being reached for is itself a friction signal — the agent
# wanted a capability that isn't a first-class tool yet.
_ESCAPE_HATCHES = {"run_command", "execute_command", "shell", "bash", "python", "exec"}
# LangGraph control-flow raised through the tool path (HITL approval, delegation,
# cancellation) is NOT friction — don't log it as a tool error.
_CONTROL_FLOW = {"GraphInterrupt", "Interrupt", "NodeInterrupt", "GraphBubbleUp",
                 "ParentCommand", "GraphDelegate", "CancelledError"}


def _ledger_path() -> Path:
    """Resolve at call time so $FRICTION_LOG and the instance dir are honored live."""
    override = os.environ.get("FRICTION_LOG")
    if override:
        return Path(override)
    base = os.environ.get("PROTOAGENT_HOME") or (Path.home() / ".protoagent")
    return Path(base) / "friction" / "friction.jsonl"


# The ledger is append-only and was unbounded (#2595). Trimmed to the newest N on write,
# amortised so the cost isn't paid every append: an agent that hits the same friction all
# day should not be able to grow this file without limit, and nothing needs the tail beyond
# "what has been getting in the way lately".
_MAX_ENTRIES = 2000
_TRIM_SLACK = 200


def _log(kind: str, summary: str, detail: str, severity: str, source: str, tool_name: str = "") -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind, "summary": summary[:200], "detail": detail[:600],
        "severity": severity, "source": source,
    }
    if tool_name:
        rec["tool"] = tool_name
    # encoding is explicit for the same reason it is everywhere else in this repo (#2521):
    # the default is the locale code page on Windows, and a friction summary quoting an
    # error with an em dash or a non-ASCII path would be written as CP1252 and read back
    # as mojibake by the UTF-8 readers below.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    _trim(path)


def _trim(path: Path) -> None:
    """Keep the ledger bounded. Rewrites only once past the cap plus slack."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= _MAX_ENTRIES + _TRIM_SLACK:
        return
    try:
        path.write_text("\n".join(lines[-_MAX_ENTRIES:]) + "\n", encoding="utf-8")
    except OSError:  # a trim failure must never break the tool call that logged
        pass


def read_entries(kind: str = "", include_resolved: bool = False) -> list[dict]:
    """Ledger records, oldest first; ``kind`` filters to one channel. Entries stamped
    ``resolved_at`` (by ``resolve_friction``) are hidden unless ``include_resolved`` —
    a resolved entry is history, not backlog."""
    path = _ledger_path()
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if kind and rec.get("kind") != kind:
            continue
        if not include_resolved and rec.get("resolved_at"):
            continue
        out.append(rec)
    return out


def grouped_entries(kind: str = "", include_resolved: bool = False) -> list[dict]:
    """Ledger records grouped by (kind, summary), newest-seen first.

    Five identical "reached for escape hatch 'shell'" entries are ONE signal repeated, not
    five separate ones — the raw log read as noise precisely because that distinction was
    lost (#2595). Each group carries ``count``, ``first_seen``/``last_seen`` and a
    representative ``detail``, which is the shape a triage list needs: what keeps happening,
    how often, and since when.
    """
    groups: dict[tuple[str, str], dict] = {}
    for idx, rec in enumerate(read_entries(kind, include_resolved)):
        key = (str(rec.get("kind", "")), str(rec.get("summary", "")))
        ts = str(rec.get("ts", ""))
        g = groups.get(key)
        if g is None:
            groups[key] = {
                "kind": key[0],
                "summary": key[1],
                "severity": rec.get("severity", "minor"),
                "source": rec.get("source", ""),
                "tool": rec.get("tool", ""),
                "detail": rec.get("detail", ""),
                "count": 1,
                "first_seen": ts,
                "last_seen": ts,
                # Set only while EVERY record in the group is resolved — one unresolved
                # occurrence means the friction is still live, however many stamped
                # records surround it.
                "resolved_at": str(rec.get("resolved_at", "")),
                # Ledger POSITION of the newest record in this group. `ts` alone cannot
                # order a burst: Windows' wall clock is coarse enough that records appended
                # in one turn share an identical isoformat string, and a stable sort then
                # preserves read order — i.e. OLDEST first, the exact inverse of what this
                # function promises (#2616). Bursts are the normal case here (five identical
                # escape-hatch entries in one turn), so the tiebreak is load-bearing, not an
                # edge case. The ledger is append-only, so a later position IS newer.
                "_last_idx": idx,
            }
            continue
        g["count"] += 1
        g["last_seen"] = max(g["last_seen"], ts)
        g["_last_idx"] = idx
        g["first_seen"] = min(g["first_seen"], ts) if g["first_seen"] else ts
        rec_resolved = str(rec.get("resolved_at", ""))
        g["resolved_at"] = max(g["resolved_at"], rec_resolved) if (g["resolved_at"] and rec_resolved) else ""
        # Keep the worst severity seen for this summary — one major occurrence makes the
        # whole group worth looking at, however many minor ones surround it.
        if _SEVERITY_RANK.get(str(rec.get("severity")), 0) > _SEVERITY_RANK.get(str(g["severity"]), 0):
            g["severity"] = rec.get("severity")
        if not g["detail"]:
            g["detail"] = rec.get("detail", "")
    ordered = sorted(groups.values(), key=lambda g: (g["last_seen"], g["_last_idx"]), reverse=True)
    for g in ordered:
        g.pop("_last_idx", None)  # bookkeeping, not part of the surface's shape
    return ordered


@tool
async def record_friction(kind: str, summary: str, detail: str = "", severity: str = "minor") -> str:
    """Record a friction point the moment you hit one — this is how the harness and the
    model get better, so don't skip it.

    kind='harness': a tool was awkward or missing, an error was confusing, or you had to
      reach for a general escape hatch (e.g. a shell tool) for something that should be a
      first-class tool → a candidate framework/tooling improvement.
    kind='model': you took a wrong path, made a mistake, or gave a weak/slow answer → this
      turn is a labeled trace worth learning from.

    Be specific: what happened, and what would have helped."""
    if kind not in _KINDS:
        return f"kind must be one of {_KINDS}"
    if severity not in _SEVERITIES:
        severity = "minor"
    if not summary.strip():
        return "summary is required (one line: what was the friction?)"
    _log(kind, summary, detail, severity, source="agent")
    return f"logged {severity} {kind} friction: “{summary}”."


@tool
async def resolve_friction(summary: str, reason: str = "") -> str:
    """Mark friction entries as resolved so they drop out of the review backlog — call this
    once the underlying rough edge is actually fixed, not to silence a live signal.

    ``summary`` is a substring match: EVERY unresolved entry whose summary contains it is
    stamped with a ``resolved_at`` timestamp (and ``reason``, if given) in place. Nothing
    is deleted — the ledger stays append-only and the audit trail survives."""
    if not summary.strip():
        return "summary is required (a substring of the entries to resolve)"
    path = _ledger_path()
    if not path.exists():
        return "no matching entries found — the friction backlog is empty."
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "no matching entries found — the ledger could not be read."
    needle = summary.strip()
    stamp = datetime.now(timezone.utc).isoformat()
    out_lines: list[str] = []
    matched = 0
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out_lines.append(line)  # foreign lines pass through untouched
            continue
        if isinstance(rec, dict) and not rec.get("resolved_at") and needle in str(rec.get("summary", "")):
            rec["resolved_at"] = stamp
            if reason.strip():
                rec["resolved_reason"] = reason.strip()[:300]
            out_lines.append(json.dumps(rec, default=str))
            matched += 1
        else:
            out_lines.append(line)
    if not matched:
        return f"no matching entries found for “{needle}”."
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return f"resolved {matched} {'entry' if matched == 1 else 'entries'} matching “{needle}”."


@tool
async def friction_review(kind: str = "", include_resolved: bool = False) -> str:
    """Review the friction backlog (the improvement leads). No kind → counts by channel +
    the most recent entries; kind='harness'|'model' → that channel's entries. Entries
    dismissed via resolve_friction are hidden unless include_resolved=True."""
    path = _ledger_path()
    if not path.exists():
        return "friction backlog is empty — nothing recorded yet."
    recs = read_entries(include_resolved=include_resolved)
    if kind in _KINDS:
        recs = [r for r in recs if r.get("kind") == kind]
    if not recs:
        return f"no {kind or ''} friction recorded."
    harness = sum(1 for r in recs if r.get("kind") == "harness")
    model = sum(1 for r in recs if r.get("kind") == "model")
    lines = [f"friction backlog: {len(recs)} total  ·  harness={harness}  model={model}", ""]
    for r in recs[-12:]:
        lines.append(f"  [{r.get('kind', '?'):<7} {r.get('severity', '?'):<5} {r.get('source', '?'):<5}] "
                     f"{r.get('summary', '')}{' [resolved]' if r.get('resolved_at') else ''}")
    return "\n".join(lines)


class FrictionMiddleware(AgentMiddleware):
    """Auto-capture: escape-hatch reaches (missing-tool signal) + genuine tool errors,
    logged without the agent's help. HITL/interrupt control-flow is filtered out."""

    def _note_escape_hatch(self, request) -> None:
        name = request.tool_call.get("name", "?")
        if name in _ESCAPE_HATCHES:
            _log("harness", f"reached for escape hatch '{name}' — candidate for a first-class tool",
                 detail=str(request.tool_call.get("args", {}))[:300], severity="minor",
                 source="auto", tool_name=name)

    def _note_error(self, request, e: Exception) -> None:
        if type(e).__name__ in _CONTROL_FLOW:
            return  # HITL pause / delegation / cancel — not friction
        _log("harness", f"tool '{request.tool_call.get('name', '?')}' raised",
             detail=f"{type(e).__name__}: {e}", severity="major", source="auto",
             tool_name=request.tool_call.get("name", ""))

    def wrap_tool_call(self, request, handler):
        self._note_escape_hatch(request)
        try:
            return handler(request)
        except Exception as e:  # noqa: BLE001 — re-raised; we only observe
            self._note_error(request, e)
            raise

    async def awrap_tool_call(self, request, handler):
        self._note_escape_hatch(request)
        try:
            return await handler(request)
        except Exception as e:  # noqa: BLE001
            self._note_error(request, e)
            raise


def _build_router():
    """``GET /api/friction`` — the read path the ledger never had (#2595).

    Agents were doing the hard part: noticing friction at the moment it happened and
    writing it down with the failing command attached. Nothing consumed it, so entries sat
    unread for weeks — two of them were filable defects, found only because an operator
    eventually opened the file by hand. An API is the smallest thing that turns a
    write-only file into something a surface, a rollup or a digest can read.
    """
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/api/friction")
    async def _friction(kind: str = "", grouped: bool = True, limit: int = 100, resolved: bool = False) -> dict:
        """Recorded friction, newest first. ``grouped`` (default) collapses repeats of the
        same summary into one item with a count — five identical escape-hatch reaches are
        one signal, and reading them as five rows is what made the raw log feel like noise.
        ``resolved`` includes entries already dismissed via resolve_friction (hidden by
        default — they're history, not backlog).
        """
        if kind and kind not in _KINDS:
            return {"error": f"kind must be one of {_KINDS}", "items": [], "total": 0}
        items = (grouped_entries(kind, include_resolved=resolved) if grouped
                 else list(reversed(read_entries(kind, include_resolved=resolved))))
        capped = items[: max(1, min(int(limit or 100), 500))]
        return {
            "items": capped,
            "total": len(items),
            "returned": len(capped),
            "grouped": bool(grouped),
            "counts": {k: sum(1 for r in read_entries(include_resolved=resolved) if r.get("kind") == k)
                       for k in _KINDS},
        }

    return router


_VIEW_PAGE = Path(__file__).parent / "view.html"


def _build_view_router():
    """``GET /plugins/friction/view`` — the console surface (#2595 D2/D3).

    The read path (#2607) turned the ledger into an API; nothing rendered it, so
    entries still sat unread unless an operator called ``friction_review`` or
    curled the route by hand. This is the "surfaces" half of #2595: a plugin
    view (ADR 0026) — a rail icon opening this page, iframed by the console.

    Served on the PUBLIC ``/plugins/friction`` prefix on purpose: an iframe
    page-load can't carry a bearer, so only the page is public chrome. Its data
    comes from ``GET /api/friction`` (unchanged, already bearer-gated by the
    default-deny middleware since it doesn't live under a public prefix) — the
    documented two-router split (docs/guides/plugin-views.md).
    """
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/view")
    async def _view():
        # Read per request (like the hello/chat_example views) — cheap, and it
        # means an operator iterating on the page never needs a restart.
        return HTMLResponse(_VIEW_PAGE.read_text(encoding="utf-8"))

    return router


def register(registry):
    """protoAgent plugin entrypoint."""
    registry.register_tools([record_friction, friction_review, resolve_friction])
    registry.register_middleware(lambda config: FrictionMiddleware())
    registry.register_router(_build_router(), prefix="")
    # Default prefix (None) resolves to /plugins/friction — the canonical public
    # view prefix (ADR 0026) — so the manifest's views[].path matches exactly.
    registry.register_router(_build_view_router())
