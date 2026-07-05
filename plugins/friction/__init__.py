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
    with path.open("a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


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


def register(registry):
    """protoAgent plugin entrypoint."""
    registry.register_tools([record_friction, friction_review])
    registry.register_middleware(lambda config: FrictionMiddleware())
