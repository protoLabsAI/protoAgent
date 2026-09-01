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
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

log = logging.getLogger(__name__)

# The live registry, captured in ``register()``. Held so the tools (which the agent calls
# far from any registry reference) can emit on the plugin's own event-bus namespace and read
# live config after a hot-reload. Module-level because a LangChain @tool is a free function.
_REGISTRY = None


def _emit(topic: str, data: dict) -> None:
    """Publish on the plugin's namespaced bus (ADR 0039), never fatally.

    Friction is the one signal in the system that says "this got in the way". Other
    plugins — a board that opens a card, a digest that rolls it up — should be able to
    hear it without importing this module, which is exactly what the bus is for."""
    if _REGISTRY is None:
        return
    try:
        _REGISTRY.emit(topic, data)
    except Exception:  # noqa: BLE001 — a bus problem must never break a tool call
        log.debug("[friction] emit(%s) failed", topic, exc_info=True)


def _cfg(key: str, default):
    """One live config read (ADR 0019), tolerant of every context this module runs in —
    tests and headless boots have no registry at all."""
    if _REGISTRY is None:
        return default
    try:
        live = _REGISTRY.live_config() if hasattr(_REGISTRY, "live_config") else _REGISTRY.config
        value = (live or {}).get(key)
        return default if value is None else value
    except Exception:  # noqa: BLE001
        return default


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


def _clip(text: str, limit: int) -> str:
    """Truncate to ``limit`` characters, saying so. A silent clip reads as a thought the
    agent failed to finish; an explicit one reads as a cap that was hit."""
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _log(kind: str, summary: str, detail: str, severity: str, source: str, tool_name: str = "") -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        # Mark a clip instead of stopping mid-word. Four of protoEngineer's 27 entries
        # ended mid-sentence with no indication anything was missing, so the report read
        # as a half-finished thought rather than a truncated one.
        "kind": kind, "summary": _clip(summary, 200), "detail": _clip(detail, 600),
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
    _emit("recorded", dict(rec))


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


def _rewrite_ledger(path: Path, lines: list[str]) -> None:
    """Replace the ledger atomically.

    ``resolve_friction`` used ``path.write_text``, which truncates before it writes:
    an interrupted rewrite left a half-empty ledger with no way back. The whole point
    of stamping ``resolved_at`` in place (rather than deleting) is that the audit trail
    survives, so the write that does it must not be the thing that loses it. Write a
    sibling temp file, then ``os.replace`` it over the original — atomic on POSIX and
    Windows alike."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def set_resolved(
    summary: str, *, resolved: bool = True, reason: str = "", kind: str = "", exact: bool = False
) -> int:
    """Stamp (or clear) ``resolved_at`` on matching entries in place; returns the count.

    ``exact`` is the difference between the two callers, and it matters. The agent's
    ``resolve_friction`` tool matches a SUBSTRING — it is fixing a rough edge it just
    described and wants every phrasing of it to drop out of the backlog. The console
    acts on one grouped row, keyed by ``(kind, summary)``; a substring match from there
    would silently resolve every OTHER row whose summary happens to contain this one's
    text (``"tool 'task' raised"`` is a substring of nothing, but
    ``"reached for escape hatch 'shell'"`` sits inside a longer agent-written summary
    the operator never looked at). The console therefore matches the full summary and
    the kind, and touches exactly the row that was clicked.

    Nothing is ever deleted — un-resolving clears the stamp, so a row reopened by
    mistake is recoverable and the ledger stays append-only in shape."""
    path = _ledger_path()
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
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
        if not isinstance(rec, dict):
            out_lines.append(line)
            continue
        rec_summary = str(rec.get("summary", ""))
        hit = rec_summary == needle if exact else needle in rec_summary
        if hit and kind and str(rec.get("kind", "")) != kind:
            hit = False
        # Only flip entries that are not already in the requested state, so the count
        # reports real changes rather than rows that were already there.
        if hit and bool(rec.get("resolved_at")) != resolved:
            if resolved:
                rec["resolved_at"] = stamp
                if reason.strip():
                    rec["resolved_reason"] = reason.strip()[:300]
            else:
                rec.pop("resolved_at", None)
                rec.pop("resolved_reason", None)
            out_lines.append(json.dumps(rec, default=str))
            matched += 1
        else:
            out_lines.append(line)
    if matched:
        _rewrite_ledger(path, out_lines)
        _emit("resolved" if resolved else "reopened",
              {"summary": needle, "kind": kind, "count": matched, "reason": reason.strip()[:300]})
    return matched


@tool
async def resolve_friction(summary: str, reason: str = "") -> str:
    """Mark friction entries as resolved so they drop out of the review backlog — call this
    once the underlying rough edge is actually fixed, not to silence a live signal.

    ``summary`` is a substring match: EVERY unresolved entry whose summary contains it is
    stamped with a ``resolved_at`` timestamp (and ``reason``, if given) in place. Nothing
    is deleted — the ledger stays append-only and the audit trail survives."""
    if not summary.strip():
        return "summary is required (a substring of the entries to resolve)"
    if not _ledger_path().exists():
        return "no matching entries found — the friction backlog is empty."
    needle = summary.strip()
    matched = set_resolved(needle, resolved=True, reason=reason)
    if not matched:
        return f"no matching entries found for \u201c{needle}\u201d."
    return f"resolved {matched} {'entry' if matched == 1 else 'entries'} matching \u201c{needle}\u201d."


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


# ── ADR 0079 seam: the agent's own backlog, in its own working state ─────────
#
# #2595 called the ledger "write-only". A read API (#2607) and a console view (#2621)
# both landed, and it stayed write-only IN THE WAY THAT MATTERED: every consumer was
# something a HUMAN had to open. The agent recorded friction and then never saw it again,
# so the same rough edge got re-reported for weeks. `register_work_provider` is the seam
# that closes it — open friction is rendered into `<working_state>` beside OPEN TASKS, so
# the agent observes its own backlog on every turn instead of polling for it.

# The provider runs INLINE ON EVERY TURN, so it must never re-read the ledger just because
# it was asked. Cache the projection and invalidate on the file's (mtime, size) — a stat is
# cheap, parsing 2000 JSONL rows is not.
_WORK_CACHE: dict = {"stamp": None, "items": []}


def _work_snapshot() -> list[dict]:
    """The grouped, unresolved ledger — recomputed only when the file actually changed."""
    path = _ledger_path()
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        _WORK_CACHE["stamp"], _WORK_CACHE["items"] = None, []
        return []
    if _WORK_CACHE["stamp"] != stamp:
        _WORK_CACHE["items"] = grouped_entries()
        _WORK_CACHE["stamp"] = stamp
    return _WORK_CACHE["items"]


def open_friction_work() -> list[dict]:
    """The friction worth interrupting the agent's turn for.

    Deliberately NOT the whole backlog. `<working_state>` is a shared, bounded budget —
    four core sections live in it — so a 23-row ledger dumped in every turn would crowd
    out the agent's actual commitments and train it to skim the block. The filter is the
    same one a person triaging would apply: something is worth carrying if it is `major`,
    or if it has happened enough times to be a pattern rather than an incident.

    A quiet instance therefore contributes NOTHING, which is the property that makes this
    safe to ship on by default: the block only grows when there is real, repeated friction.
    """
    if not bool(_cfg("working_state", True)):
        return []
    threshold = max(1, int(_cfg("working_state_repeat_threshold", 3) or 3))
    limit = max(1, int(_cfg("working_state_limit", 3) or 3))
    ranked = sorted(
        (g for g in _work_snapshot()
         if g.get("severity") == "major" or int(g.get("count") or 1) >= threshold),
        key=lambda g: (_SEVERITY_RANK.get(str(g.get("severity")), 0), int(g.get("count") or 1),
                       str(g.get("last_seen") or "")),
        reverse=True,
    )
    out: list[dict] = []
    for g in ranked[:limit]:
        count = int(g.get("count") or 1)
        state = str(g.get("severity") or "minor")
        if count > 1:
            state += f" x{count}"
        out.append({
            "state": state,
            "title": str(g.get("summary") or ""),
            # The hint names the escape hatch, because "what would have helped" is the
            # actionable half and it is the half the agent is being asked to fix.
            "hint": f"tool: {g['tool']}" if g.get("tool") else "resolve_friction when fixed",
        })
    return out


class FrictionMiddleware(AgentMiddleware):
    """Auto-capture: escape-hatch reaches (missing-tool signal) + genuine tool errors,
    logged without the agent's help. HITL/interrupt control-flow is filtered out."""

    def _note_escape_hatch(self, request) -> None:
        name = request.tool_call.get("name", "?")
        if name in _ESCAPE_HATCHES:
            # json.dumps, not str(): a Python dict repr ({'command': 'git diff'}) is not
            # parseable by anything downstream, and the console rendered it verbatim —
            # single quotes, u-prefixes and all — as the "detail" an operator is meant to
            # read. JSON is the same information the view can pretty-print.
            try:
                args = json.dumps(request.tool_call.get("args", {}), default=str)
            except (TypeError, ValueError):
                args = str(request.tool_call.get("args", {}))
            _log("harness", f"reached for escape hatch '{name}' — candidate for a first-class tool",
                 detail=args[:300], severity="minor", source="auto", tool_name=name)

    def _note_error(self, request, e: Exception) -> None:
        if type(e).__name__ in _CONTROL_FLOW:
            return  # HITL pause / delegation / cancel — not friction
        # The exception type belongs in the SUMMARY, not only the detail. Groups are keyed
        # on (kind, summary), so a bare "tool 'task' raised" collapsed every distinct
        # failure of that tool into ONE row — a RuntimeError and a TimeoutError counted
        # together, showing "x5" against whichever detail happened to be logged first. The
        # count is the triage signal, so a count that spans unrelated bugs is worse than no
        # count: it argues for a fix nobody can scope.
        _log("harness", f"tool '{request.tool_call.get('name', '?')}' raised {type(e).__name__}",
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


def _build_subagent():
    """A read-only triage delegate the lead agent can dispatch with ``task``.

    Triage is a genuinely different job from recording: it reads the WHOLE backlog at
    once, looks for the pattern across entries, and decides what is worth filing. Doing
    that inline costs the lead agent a long context of raw ledger rows mid-task, which is
    exactly what delegation is for.

    Read-only by construction — it gets `friction_review` and nothing that writes. A
    triage pass must not be able to resolve what it just decided to file; that is the
    operator's call (`/friction`) or a deliberate `resolve_friction` after the fix lands.
    """
    from graph.subagents.config import SubagentConfig

    return SubagentConfig(
        name="friction_triage",
        description=(
            "Read the friction backlog and turn it into a filing plan: group related "
            "entries, name the root cause, and draft issue titles/bodies for the ones "
            "worth tracking. Read-only — it never resolves or records."
        ),
        system_prompt=(
            "You triage this agent's own friction backlog.\n\n"
            "Call `friction_review` (both channels) and read the whole list before "
            "judging any single entry — the value is in the pattern. Then:\n"
            "1. GROUP entries that share a root cause, even when the summaries differ. "
            "Five 'reached for escape hatch' entries and one 'no tool to do X' are "
            "usually one missing tool.\n"
            "2. RANK by cost: how often it recurs x how much it blocked. A major seen "
            "once can outrank a minor seen ten times, or not — say which and why.\n"
            "3. For each group worth tracking, draft a title and a body: what happens, "
            "how to reproduce it, and what would have helped. The last part is the "
            "actionable half and the half that gets dropped.\n"
            "4. Say explicitly which entries are NOT worth filing, and why — a triage "
            "pass that files everything has not triaged anything.\n\n"
            "You cannot resolve anything and should not ask to. Report the plan and stop."
        ),
        # Explicitly listed: a subagent gets only the tools named here, so an empty list
        # would leave it unable to read the very backlog it exists to triage.
        tools=["friction_review"],
        default_prompt="Triage the current friction backlog and propose what to file.",
    )


async def _friction_command(rest: str, _session_id: str):
    """``/friction`` — the operator's read of the backlog, without spending a turn.

    User-only by design (``register_chat_command`` is not an agent tool), which is the
    point: an operator can RESOLVE friction from here, and the model cannot resolve its
    own backlog by talking to itself. ``/friction`` summarises; ``/friction <text>``
    resolves every entry matching that text and says how many it stamped.
    """
    needle = rest.strip()
    if needle:
        changed = set_resolved(needle, resolved=True, reason="resolved by the operator via /friction")
        if not changed:
            return f"No open friction matching “{needle}”. `/friction` lists what is open."
        return f"Resolved {changed} {'entry' if changed == 1 else 'entries'} matching “{needle}”."

    groups = grouped_entries()
    if not groups:
        return "**Friction backlog is empty** — nothing recorded, or everything is resolved."
    occurrences = sum(int(g.get("count") or 1) for g in groups)
    major = [g for g in groups if g.get("severity") == "major"]
    lines = [
        f"**Friction backlog** — {len(groups)} open "
        f"{'signal' if len(groups) == 1 else 'signals'} across {occurrences} "
        f"{'occurrence' if occurrences == 1 else 'occurrences'}"
        + (f", {len(major)} major" if major else ""),
        "",
    ]
    # Worst first — the same ranking the working-state projection uses, so the operator
    # and the agent are looking at the same top of the list.
    ranked = sorted(
        groups,
        key=lambda g: (_SEVERITY_RANK.get(str(g.get("severity")), 0), int(g.get("count") or 1)),
        reverse=True,
    )
    for g in ranked[:10]:
        count = int(g.get("count") or 1)
        tail = f" ×{count}" if count > 1 else ""
        lines.append(f"- `{g.get('severity', 'minor')}`{tail} {g.get('summary', '')}")
    if len(ranked) > 10:
        lines.append(f"- …and {len(ranked) - 10} more")
    lines += ["", "Open the **Friction** view to read details, or `/friction <text>` to resolve."]
    return "\n".join(lines)


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

    @router.post("/api/friction/resolve")
    async def _resolve(payload: dict) -> dict:
        """Resolve (or reopen) one grouped row — the console's half of the triage state.

        The view used to "dismiss" into ``localStorage``: per-browser, invisible to the
        agent, and contradicted by the ledger the moment ``friction_review`` ran. An
        operator would clear the backlog on screen and the agent would keep reporting
        every item as live. There is one backlog, so there is one place to record that a
        row is done — the ledger, the same ``resolved_at`` stamp the ``resolve_friction``
        tool writes.

        Matches the full ``summary`` and ``kind`` exactly (see ``set_resolved``): the
        console is acting on the row it rendered, not on a search.
        """
        summary = str(payload.get("summary") or "").strip()
        if not summary:
            return {"error": "summary is required", "changed": 0}
        kind = str(payload.get("kind") or "").strip()
        if kind and kind not in _KINDS:
            return {"error": f"kind must be one of {_KINDS}", "changed": 0}
        resolved = bool(payload.get("resolved", True))
        changed = set_resolved(
            summary,
            resolved=resolved,
            reason=str(payload.get("reason") or ""),
            kind=kind,
            exact=True,
        )
        return {"changed": changed, "resolved": resolved, "summary": summary, "kind": kind}

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
    global _REGISTRY
    _REGISTRY = registry
    registry.register_tools([record_friction, friction_review, resolve_friction])
    registry.register_middleware(lambda config: FrictionMiddleware())
    # ADR 0079 — the agent observes its own backlog instead of re-reporting it (see
    # ``open_friction_work``). Bounded and self-limiting: a quiet ledger adds no lines.
    registry.register_work_provider("backlog", open_friction_work, label="OPEN FRICTION")
    # The tools were always here; what was missing was the judgement for USING them. The
    # module docstring said as much ("the model has to reach for it") and then shipped no
    # skill, leaving every operator to paste the same guidance into their own persona.
    registry.register_skill_dir("skills")
    # User-only (never an agent tool) so resolving stays an operator action — the model
    # must not be able to clear its own backlog by deciding it is clear.
    registry.register_chat_command("friction", _friction_command)
    registry.register_subagent(_build_subagent())
    # Advertised on the agent card so a PEER can ask this agent what has been getting in
    # its way — the fleet-level version of the same question the console view answers.
    registry.register_a2a_skill({
        "id": "friction-review",
        "name": "Friction review",
        "description": (
            "Report this agent's recorded friction — the tools that were missing or "
            "awkward, the errors that misled it, and how often each recurred."
        ),
        "tags": ["diagnostics", "self-report", "observability"],
        "examples": ["What has been getting in your way lately?"],
    })
    registry.register_router(_build_router(), prefix="")
    # Default prefix (None) resolves to /plugins/friction — the canonical public
    # view prefix (ADR 0026) — so the manifest's views[].path matches exactly.
    registry.register_router(_build_view_router())
