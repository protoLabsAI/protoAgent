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
`"model"` → a labeled trace worth learning from. `friction_review` surfaces it. Enable
via `plugins: { enabled: [friction] }`. The ledger path is `$FRICTION_LOG` or, by
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


def read_entries(kind: str = "") -> list[dict]:
    """Every ledger record, oldest first; ``kind`` filters to one channel."""
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
        if isinstance(rec, dict) and (not kind or rec.get("kind") == kind):
            out.append(rec)
    return out


def grouped_entries(kind: str = "") -> list[dict]:
    """Ledger records grouped by (kind, summary), newest-seen first.

    Five identical "reached for escape hatch 'shell'" entries are ONE signal repeated, not
    five separate ones — the raw log read as noise precisely because that distinction was
    lost (#2595). Each group carries ``count``, ``first_seen``/``last_seen`` and a
    representative ``detail``, which is the shape a triage list needs: what keeps happening,
    how often, and since when.
    """
    groups: dict[tuple[str, str], dict] = {}
    for rec in read_entries(kind):
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
            }
            continue
        g["count"] += 1
        g["last_seen"] = max(g["last_seen"], ts)
        g["first_seen"] = min(g["first_seen"], ts) if g["first_seen"] else ts
        # Keep the worst severity seen for this summary — one major occurrence makes the
        # whole group worth looking at, however many minor ones surround it.
        if _SEVERITY_RANK.get(str(rec.get("severity")), 0) > _SEVERITY_RANK.get(str(g["severity"]), 0):
            g["severity"] = rec.get("severity")
        if not g["detail"]:
            g["detail"] = rec.get("detail", "")
    return sorted(groups.values(), key=lambda g: g["last_seen"], reverse=True)


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
async def friction_review(kind: str = "") -> str:
    """Review the friction backlog (the improvement leads). No kind → counts by channel +
    the most recent entries; kind='harness'|'model' → that channel's entries."""
    path = _ledger_path()
    if not path.exists():
        return "friction backlog is empty — nothing recorded yet."
    recs = []
    for line in path.read_text().splitlines():
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if kind in _KINDS:
        recs = [r for r in recs if r.get("kind") == kind]
    if not recs:
        return f"no {kind or ''} friction recorded."
    harness = sum(1 for r in recs if r.get("kind") == "harness")
    model = sum(1 for r in recs if r.get("kind") == "model")
    lines = [f"friction backlog: {len(recs)} total  ·  harness={harness}  model={model}", ""]
    for r in recs[-12:]:
        lines.append(f"  [{r.get('kind', '?'):<7} {r.get('severity', '?'):<5} {r.get('source', '?'):<5}] "
                     f"{r.get('summary', '')}")
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
    async def _friction(kind: str = "", grouped: bool = True, limit: int = 100) -> dict:
        """Recorded friction, newest first. ``grouped`` (default) collapses repeats of the
        same summary into one item with a count — five identical escape-hatch reaches are
        one signal, and reading them as five rows is what made the raw log feel like noise.
        """
        if kind and kind not in _KINDS:
            return {"error": f"kind must be one of {_KINDS}", "items": [], "total": 0}
        items = grouped_entries(kind) if grouped else list(reversed(read_entries(kind)))
        capped = items[: max(1, min(int(limit or 100), 500))]
        return {
            "items": capped,
            "total": len(items),
            "returned": len(capped),
            "grouped": bool(grouped),
            "counts": {k: sum(1 for r in read_entries() if r.get("kind") == k) for k in _KINDS},
        }

    return router


def register(registry):
    """protoAgent plugin entrypoint."""
    registry.register_tools([record_friction, friction_review])
    registry.register_middleware(lambda config: FrictionMiddleware())
    registry.register_router(_build_router(), prefix="")
