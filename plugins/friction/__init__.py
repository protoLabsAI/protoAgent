"""Friction Log — the agent records its own rough edges; the backlog writes itself.

A self-report pattern in the spirit of NousResearch/hermes-agent: a self-improving
agent captures where it hit friction and feeds that back into improving its harness
(tools/framework) and its model (training signal). Two capture channels, one ledger:

  * AGENT-INITIATED — `record_friction`: the agent flags what a detector can't see —
    a missing or awkward tool, a confusing error, reaching for a general escape hatch
    (a shell tool) for something that should be first-class, a wrong path it recognizes.
    High-signal: the model knows when it's frustrated. (Prompt it to use this in your
    agent's persona/system prompt — the tool exists, but the model has to reach for it.)
  * AUTO-CAPTURE — `FrictionMiddleware.wrap_tool_call`: genuine tool errors, and shell
    commands that duplicate a first-class tool the agent has bound (`cat` via run_command
    when `read_file` is there — see `_FIRST_CLASS_EQUIVALENTS`), with no agent effort.
    `git`, test runners and builds through a shell tool are real work, not friction.
    HITL/interrupt control-flow is filtered out (a tool pausing for approval or
    delegating is not friction).

`kind` splits the backlog: `"harness"` → an improvement to the tools/framework;
`"model"` → a labeled trace worth learning from. `friction_review` surfaces it;
`resolve_friction` dismisses entries once fixed (a `resolved_at` stamp in place — the
ledger stays append-only, so the audit trail survives).

Nothing here waits for someone to go looking. Open friction is projected into the agent's
`<working_state>` (ADR 0079), broadcast on the plugin's event bus (ADR 0039), reviewable by
the operator with `/friction`, and triaged by the read-only `friction_triage` subagent. A
`recording-friction` SKILL.md teaches WHEN to record — the tools were always here; the
judgement for using them was the missing half.

First-party, **on by default**; disable via `plugins: { disabled: [friction] }`. The ledger
path is `$FRICTION_LOG` or, by default, `instance_paths().store("friction")`.
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
        live = _REGISTRY.live_config()  # already falls back to the register-time snapshot
        value = (live or {}).get(key)
        return default if value is None else value
    except Exception:  # noqa: BLE001
        return default


_KINDS = ("harness", "model")
_SEVERITIES = ("minor", "major")
_SEVERITY_RANK = {"minor": 1, "major": 2}


def _issue_repo() -> str:
    """The ``owner/name`` this instance files friction against, or "" if it can't tell.

    ONE source: this plugin's own ``issue_repo`` when an operator has pinned one. Empty
    means the view offers copy-to-clipboard instead of a prefilled-issue link.

    It used to fall back to the FIRST managed project's ``github`` (ADR 0095). That was
    wrong for the friction this ledger mostly holds: harness friction is about the agent
    RUNTIME — its tools, errors and framework — not about whatever repo the agent happens
    to be working on. A coding agent that onboards someone else's repo (a private
    interview repo, a client's project) would have had its harness friction pointed at
    that repo, which is filing protoAgent's bugs in a stranger's tracker. The view files
    every row against one repo, with no per-kind split, so there is no row the registry
    answer is right for; a guessed repo is worse than a paste.

    Deliberately NOT the github plugin's ``default_repo`` either: plugins coordinate
    through the host, never by reading each other's config (ADR 0039)."""
    pinned = str(_cfg("issue_repo", "") or "").strip()
    if pinned:
        return pinned.removeprefix("https://github.com/").strip("/")
    return ""


def _triage_rank(group: dict) -> tuple:
    """Worst-first ordering, defined ONCE. The operator's `/friction` list and the agent's
    working-state projection have to agree about what the top of the backlog is, or the two
    of them read different lists off the same ledger."""
    return (
        _SEVERITY_RANK.get(str(group.get("severity")), 0),
        int(group.get("count") or 1),
        str(group.get("last_seen") or ""),
    )

# General shell/exec tools. Reaching for one is only SOMETIMES friction — see
# ``_duplicated_tool``: a shell hatch is friction when the command duplicates a
# first-class tool the agent has bound, never for `git diff` or `npm test`.
_ESCAPE_HATCHES = {"run_command", "execute_command", "shell", "bash", "python", "exec"}
# The hatches whose args carry a SHELL COMMAND we can parse. `python`/`exec` run code,
# which has no first-word to classify, so they keep the old always-log behaviour.
_SHELL_HATCHES = {"run_command", "execute_command", "shell", "bash"}
_COMMAND_ARG_KEYS = ("command", "cmd", "script")

# ── Which shell commands duplicate a first-class tool ─────────────────────────
#
# navaEngineer (a hands-on coding agent) had 131 auto entries, EVERY one "reached for
# escape hatch 'run_command'". 127 of them were git, test runners, builds and package
# managers — legitimate work with no first-class equivalent — and only 4 (`ls`, `cat`,
# `grep`) duplicated a tool it already had. Logging every reach made the signal ~97%
# noise, and the working-state projection then told the agent "minor x131: reached for
# escape hatch 'run_command'" every turn, nudging it away from its main tool.
#
# So the table below is the WHOLE definition of escape-hatch friction for a shell
# hatch: the command's first word (after wrappers are stripped — see ``_command_word``)
# is on this list AND the tool it maps to is actually bound for this agent. Rows are
# checked in order, so the redirect row for `cat > f` wins over `cat` → read_file.
# Conservative on purpose: a row that fires on real work is the bug this fixes.


def _has_redirect(tokens: list[str]) -> bool:
    """A stdout redirect into a file — not ``2>/dev/null`` or ``2> err.log``."""
    for i, t in enumerate(tokens):
        if t not in (">", ">>"):
            continue
        if i > 0 and tokens[i - 1].isdigit() and tokens[i - 1] != "1":
            continue  # an fd redirect (stderr), not the command's output
        if i + 1 < len(tokens) and tokens[i + 1] == "/dev/null":
            continue
        return True
    return False


def _sed_in_place(tokens: list[str]) -> bool:
    # `-i`, `-i.bak`, `-Ei`, `--in-place[=SUFFIX]`. No other sed short flag is `i`.
    return any(
        t.startswith("--in-place") or (t.startswith("-") and not t.startswith("--") and "i" in t[1:])
        for t in tokens[1:]
    )


def _awk_in_place(tokens: list[str]) -> bool:
    # gawk's `-i inplace` (or `--include=inplace`); a plain awk program is a read.
    return "inplace" in tokens or any(t in ("--include=inplace", "-iinplace") for t in tokens)


# (command words, the first-class tool that does this, extra condition or None)
_FIRST_CLASS_EQUIVALENTS: tuple[tuple[frozenset[str], str, object], ...] = (
    (frozenset({"cat", "echo", "printf"}), "write_file", _has_redirect),
    (frozenset({"cat", "head", "tail", "less", "more"}), "read_file", None),
    (frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"}), "search_files", None),
    (frozenset({"ls", "tree"}), "list_dir", None),
    (frozenset({"find", "fd"}), "find_files", None),
    (frozenset({"sed"}), "edit_file", _sed_in_place),
    (frozenset({"awk", "gawk"}), "edit_file", _awk_in_place),
    (frozenset({"tee"}), "write_file", None),
)

# Prefixes that run the REAL command: `sudo cat`, `env FOO=1 grep`, `time npm test`.
_WRAPPERS = {"sudo", "env", "time", "nohup", "command", "nice"}
# Wrapper flags that take a separate VALUE: `nice -n 10 cat`, `sudo -u app cat`,
# `env -u HOME grep`. Without these the value would be read as the command.
_WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-C", "-D", "-U", "-p", "-h", "-r", "-t"},
    "env": {"-u", "-C"},
    "nice": {"-n"},
}
_SEGMENT_OPERATORS = {"&&", "||", ";", "|", "&", "\n"}


def _split_shell(command: str) -> list[str]:
    import shlex

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes — a best-effort split beats giving up
        return command.split()


def _command_word(command: str, _depth: int = 0) -> tuple[str, list[str]]:
    """The command a shell line actually runs, and that command's tokens.

    Strips what is not the command: leading ``cd X &&`` / ``cd X;`` hops, ``VAR=value``
    assignments, wrappers (``sudo``, ``env …``, ``time``), ``mise exec … --`` and
    ``sh -c '…'``. For a pipeline or a chain, only the FIRST remaining segment counts —
    ``cat foo | grep x`` is a read, ``git diff | grep x`` is git."""
    tokens = _split_shell(command)
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SEGMENT_OPERATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    segments = [s for s in segments if s]
    # Drop directory hops: `cd x && cat y` reads y.
    while len(segments) > 1 and segments[0][0] in ("cd", "pushd"):
        segments.pop(0)
    if not segments:
        return "", []
    seg = segments[0]
    i = 0
    while i < len(seg):
        tok = seg[i]
        if "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            i += 1  # VAR=value
        elif tok in _WRAPPERS:
            i += 1
            while i < len(seg) and (seg[i].startswith("-") or ("=" in seg[i] and seg[i].split("=", 1)[0].isidentifier())):
                # the wrapper's own flags / env assignments, and a flag's separate value
                i += 2 if seg[i] in _WRAPPER_VALUE_FLAGS.get(tok, ()) else 1
        elif tok == "mise" and i + 1 < len(seg) and seg[i + 1] in ("exec", "x"):
            if "--" not in seg[i:]:
                break  # `mise exec` without `--` is mise's own business
            i = seg.index("--", i) + 1
        elif (
            tok.rsplit("/", 1)[-1] in ("sh", "bash", "zsh")
            and i + 2 < len(seg)
            and seg[i + 1] in ("-c", "-lc", "-ic")
            and _depth < 3
        ):
            return _command_word(seg[i + 2], _depth + 1)
        else:
            break
    rest = seg[i:]
    if not rest:
        return "", []
    return rest[0].rsplit("/", 1)[-1], rest


def _duplicated_tool(command: str, bound: set[str] | frozenset[str]) -> tuple[str, str] | None:
    """``(command word, first-class tool)`` when ``command`` duplicates a BOUND tool.

    None for everything else — git, test runners, builds, package managers, project
    scripts — and for a duplicate whose first-class tool this agent does not have: an
    agent with no ``list_dir`` running ``ls`` is using the only tool it has, which is
    not friction."""
    word, tokens = _command_word(command)
    if not word:
        return None
    for words, tool_name, condition in _FIRST_CLASS_EQUIVALENTS:
        if word in words and (condition is None or condition(tokens)):
            return (word, tool_name) if tool_name in bound else None
    return None


def _command_arg(args) -> str | None:
    """The shell command a hatch was called with, or None if its args carry none."""
    if not isinstance(args, dict):
        return None
    for key in _COMMAND_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            import shlex

            return shlex.join(value)
    return None


def _count_bucket(count: int) -> str:
    """A working-state count that doesn't change on every occurrence.

    The projection is re-rendered into the prompt every turn; an exact count ("x131",
    "x132", …) rewrote that line on every call — churning the prompt text (and its
    cache) to say nothing new. Small counts stay exact (x3 is meaningfully different
    from x7); past ten, the order of magnitude is the signal: x10+, x100+, x1000+.
    Exact counts stay in the ledger, the view and ``/friction``."""
    if count < 10:
        return f"x{count}"
    bucket = 10
    while bucket * 10 <= count:
        bucket *= 10
    return f"x{bucket}+"
# LangGraph control-flow raised through the tool path (HITL approval, delegation,
# cancellation) is NOT friction — don't log it as a tool error.
_CONTROL_FLOW = {"GraphInterrupt", "Interrupt", "NodeInterrupt", "GraphBubbleUp",
                 "ParentCommand", "GraphDelegate", "CancelledError"}


def _legacy_ledger_path() -> Path:
    """Where the ledger lived before it was instance-scoped. Read-only, for migration."""
    base = os.environ.get("PROTOAGENT_HOME") or (Path.home() / ".protoagent")
    return Path(base) / "friction" / "friction.jsonl"


def _ledger_path() -> Path:
    """Resolve at call time so $FRICTION_LOG and the instance dir are honored live.

    Via ``instance_paths()`` (ADR 0004), NOT by re-deriving a path from
    ``PROTOAGENT_HOME``. The old guess was ``<PROTOAGENT_HOME or ~/.protoagent>/friction/``,
    which is only correct on the desktop — there ``PROTOAGENT_HOME`` points at one
    workspace, so ``instance_root`` IS that directory and the two agree. On every other
    install the instance root is ``~/.protoagent/default`` (or ``…/dev``), so the ledger
    landed one level ABOVE it: outside the instance tree, shared by every instance on the
    box, and untouched by ``scripts/dev-reset.sh``, which wipes only the sandbox.

    It went unnoticed because the agent that uses this most is a desktop workspace. It
    starts mattering now that the plugin is on by default and every instance writes."""
    override = os.environ.get("FRICTION_LOG")
    if override:
        return Path(override)
    try:
        from infra.paths import instance_paths

        path = Path(instance_paths().store("friction")) / "friction.jsonl"
    except Exception:  # noqa: BLE001 — a path-resolution failure must not disable recording
        return _legacy_ledger_path()
    # Adopt an existing pre-instance-scoping ledger rather than silently starting a second
    # one: an operator who upgrades should keep their history, not appear to have none.
    if not path.exists():
        legacy = _legacy_ledger_path()
        if legacy.is_file() and legacy != path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                legacy.replace(path)
                log.info("[friction] migrated ledger %s -> %s", legacy, path)
            except OSError:
                return legacy  # can't move it — keep using it where it is
    return path


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


def _log(
    kind: str, summary: str, detail: str, severity: str, source: str, tool_name: str = "", suggest: str = ""
) -> None:
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
    if suggest:
        rec["suggest"] = suggest  # the first-class tool an escape-hatch command duplicated
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
                "suggest": rec.get("suggest", ""),
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
                    rec["resolved_reason"] = _clip(reason.strip(), 300)
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
              {"summary": needle, "kind": kind, "count": matched, "reason": _clip(reason.strip(), 300)})
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
        key=_triage_rank, reverse=True,
    )
    out: list[dict] = []
    for g in ranked[:limit]:
        count = int(g.get("count") or 1)
        state = str(g.get("severity") or "minor")
        if count > 1:
            state += f" {_count_bucket(count)}"
        if g.get("suggest"):
            # An escape-hatch duplicate: the fix is the agent's own — use the tool it has.
            hint = f"use {g['suggest']}"
        elif g.get("tool"):
            # The hint names the escape hatch, because "what would have helped" is the
            # actionable half and it is the half the agent is being asked to fix.
            hint = f"tool: {g['tool']}"
        else:
            hint = "resolve_friction when fixed"
        out.append({"state": state, "title": str(g.get("summary") or ""), "hint": hint})
    return out


class FrictionMiddleware(AgentMiddleware):
    """Auto-capture: escape-hatch reaches (missing-tool signal) + genuine tool errors,
    logged without the agent's help. HITL/interrupt control-flow is filtered out.

    Also a pass-through ``wrap_model_call``, used ONLY to learn which tools are bound: a
    tool call carries no view of the toolset, and "``ls`` via run_command" is friction
    only when ``list_dir`` is actually there to use. The set accumulates across model
    calls (a deferred tool that surfaces later still counts as available)."""

    def __init__(self, bound_tools=None) -> None:
        super().__init__()
        self._bound_tools: set[str] = set(bound_tools or ())

    def observe_tools(self, tools) -> None:
        """Record the names of the tools a model call was bound with."""
        for t in tools or ():
            if isinstance(t, dict):
                name = t.get("name") or (t.get("function") or {}).get("name")
            else:
                name = getattr(t, "name", None)
            if isinstance(name, str) and name:
                self._bound_tools.add(name)

    def wrap_model_call(self, request, handler):
        self.observe_tools(getattr(request, "tools", None))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        self.observe_tools(getattr(request, "tools", None))
        return await handler(request)

    def _note_escape_hatch(self, request) -> None:
        name = request.tool_call.get("name", "?")
        if name not in _ESCAPE_HATCHES:
            return
        exempt = _cfg("escape_hatch_exempt", []) or []
        if isinstance(exempt, str):  # a hand-edited "a, b" is the same intent as a list
            exempt = [p.strip() for p in exempt.split(",")]
        if name in {str(e).strip() for e in exempt}:
            return
        raw_args = request.tool_call.get("args", {})
        # json.dumps, not str(): a Python dict repr ({'command': 'git diff'}) is not
        # parseable by anything downstream, and the console rendered it verbatim —
        # single quotes, u-prefixes and all — as the "detail" an operator is meant to
        # read. JSON is the same information the view can pretty-print.
        try:
            args = json.dumps(raw_args, default=str)
        except (TypeError, ValueError):
            args = str(raw_args)
        command = _command_arg(raw_args) if name in _SHELL_HATCHES else None
        if command is not None:
            hit = _duplicated_tool(command, self._bound_tools)
            if hit is None:
                return  # git, a test runner, a build — real work, not friction
            word, suggest = hit
            _log("harness", f"used `{word}` via {name} — {suggest} does this",
                 detail=_clip(args, 300), severity="minor", source="auto",
                 tool_name=name, suggest=suggest)
            return
        # No command to classify (`python`/`exec`, or a shell hatch with an unknown arg
        # shape): the original, coarser signal.
        _log("harness", f"reached for escape hatch '{name}' — candidate for a first-class tool",
             detail=_clip(args, 300), severity="minor", source="auto", tool_name=name)

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
        # Substring, like the tool — an operator clearing "board_cancel" means every
        # phrasing of it. But a bulk action whose blast radius is invisible is how you
        # resolve six signals meaning to resolve one, so NAME what went. Each row can be
        # reopened individually from the Friction view; nothing is ever deleted.
        hit = [g for g in grouped_entries() if needle in str(g.get("summary", ""))]
        changed = set_resolved(needle, resolved=True, reason="resolved by the operator via /friction")
        if not changed:
            return f"No open friction matching “{needle}”. `/friction` lists what is open."
        lines = [
            f"Resolved **{changed}** {'entry' if changed == 1 else 'entries'} across "
            f"{len(hit)} {'signal' if len(hit) == 1 else 'signals'} matching “{needle}”:",
            "",
        ]
        lines += [f"- {g.get('summary', '')}" for g in hit[:10]]
        if len(hit) > 10:
            lines.append(f"- …and {len(hit) - 10} more")
        lines += ["", "Reopen any of them from the **Friction** view if that was too broad."]
        return "\n".join(lines)

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
    ranked = sorted(groups, key=_triage_rank, reverse=True)
    for g in ranked[:10]:
        count = int(g.get("count") or 1)
        tail = f" ×{count}" if count > 1 else ""
        lines.append(f"- `{g.get('severity', 'minor')}`{tail} {g.get('summary', '')}")
    if len(ranked) > 10:
        lines.append(f"- …and {len(ranked) - 10} more")
    lines += ["", "Open the **Friction** view to read details, or `/friction <text>` to resolve."]
    return "\n".join(lines)


def _build_router(*, legacy: bool = False):
    """The ledger's read + triage API — the read path it never had (#2595).

    Mounted TWICE, on purpose. The canonical mount is the namespaced
    ``/api/plugins/friction/...`` that plugin-view Rule 2 asks for
    (docs/guides/building-react-plugin-views.md); ``legacy=True`` re-serves the same
    handlers at the top-level ``/api/friction`` that #2607 shipped and documented, so
    anything already calling it keeps working. Both are bearer-gated — they live under
    ``/api/`` — and the console view calls the canonical one.

    Agents were doing the hard part: noticing friction at the moment it happened and
    writing it down with the failing command attached. Nothing consumed it, so entries sat
    unread for weeks — two of them were filable defects, found only because an operator
    eventually opened the file by hand. An API is the smallest thing that turns a
    write-only file into something a surface, a rollup or a digest can read.
    """
    from fastapi import APIRouter

    router = APIRouter()
    # The prefix is applied by register_router for the canonical mount, so the paths here
    # are relative; the legacy mount carries the old absolute paths itself.
    read_path = "/api/friction" if legacy else ""
    resolve_path = "/api/friction/resolve" if legacy else "/resolve"

    @router.get(read_path or "/")
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
            # "" when this instance can't tell which repo it files against — the view then
            # offers copy-to-clipboard instead of a link that would 404.
            "issue_repo": _issue_repo(),
            "counts": {k: sum(1 for r in read_entries(include_resolved=resolved) if r.get("kind") == k)
                       for k in _KINDS},
        }

    @router.get("/api/friction/fleet" if legacy else "/fleet")
    async def _fleet(force: bool = False) -> dict:
        """Friction across the hub's members (#2595 acceptance 2).

        TTL-cached: the view refetches on every filter change, and a fan-out across the
        roster is not something to repeat on a keystroke."""
        import time

        now = time.monotonic()
        if not force and _FLEET_CACHE["data"] is not None and (now - _FLEET_CACHE["at"]) < _FLEET_TTL_S:
            return {**_FLEET_CACHE["data"], "cached": True}
        data = await fleet_rollup()
        _FLEET_CACHE.update(at=now, data=data)
        return {**data, "cached": False}

    @router.post(resolve_path)
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


# ── #2595 acceptance 2: friction per member, without opening files on disk ──
#
# Every member records its own friction into its own ledger, so the systemic signal — the
# rough edge that is costing the WHOLE fleet, not one agent — was only visible by opening
# each member's view in turn and holding the comparison in your head. This fans out to the
# hub-supervised roster (ADR 0042) and merges by (kind, summary), keeping per-member
# attribution so "12 times" and "across 4 agents" are both answerable.

_FLEET_TTL_S = 30.0
_FLEET_CACHE: dict = {"at": 0.0, "data": None}


async def _member_friction(client, member: dict) -> dict | None:
    """One member's grouped backlog, or None if it can't be reached.

    Calls the LEGACY top-level ``/api/friction``, not the namespaced path this plugin now
    prefers: a hub is routinely newer than its members, and the old path is the one that
    exists in both. Asking for the new one would make the rollup silently skip every member
    that has not been rolled yet — which is exactly the fleet a rollup is for."""
    base = str(member.get("url") or "").rstrip("/")
    if not base:
        return None
    headers = {}
    token = str(member.get("token") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = await client.get(f"{base}/api/friction",
                                params={"grouped": "true", "limit": 200}, headers=headers)
        if resp.status_code != 200:
            return None
        body = resp.json()
    except Exception:  # noqa: BLE001 — one unreachable member must not fail the rollup
        return None
    if not isinstance(body, dict):
        return None
    return {
        "name": str(member.get("name") or member.get("id") or base),
        "items": [i for i in (body.get("items") or []) if isinstance(i, dict)],
    }


async def fleet_rollup(*, timeout_s: float = 4.0) -> dict:
    """Friction across the fleet, merged by (kind, summary) with per-member attribution."""
    try:
        from graph.fleet.supervisor import list_remotes

        members = [m for m in list_remotes() if isinstance(m, dict) and m.get("url")]
    except Exception:  # noqa: BLE001 — not a hub, or the fleet module isn't available
        members = []
    if not members:
        return {"items": [], "members": [], "unreachable": [], "is_hub": False}

    import asyncio

    import httpx

    # An explicit read timeout is not optional here: the fleet proxy has none of its own,
    # and a member parked on a slow read would otherwise hold this request open forever.
    limits = httpx.Timeout(timeout_s, connect=2.0)
    async with httpx.AsyncClient(timeout=limits, follow_redirects=False) as client:
        results = await asyncio.gather(
            *[_member_friction(client, m) for m in members], return_exceptions=True
        )

    merged: dict[tuple[str, str], dict] = {}
    reached, unreachable = [], []
    for member, result in zip(members, results):
        name = str(member.get("name") or member.get("id") or "")
        if not isinstance(result, dict):
            unreachable.append(name)
            continue
        reached.append(result["name"])
        for item in result["items"]:
            key = (str(item.get("kind", "")), str(item.get("summary", "")))
            row = merged.get(key)
            if row is None:
                row = merged[key] = {
                    "kind": key[0], "summary": key[1],
                    "severity": item.get("severity", "minor"),
                    "source": item.get("source", ""), "tool": item.get("tool", ""),
                    "detail": item.get("detail", ""), "count": 0,
                    "first_seen": str(item.get("first_seen") or ""),
                    "last_seen": str(item.get("last_seen") or ""),
                    "resolved_at": "", "agents": [],
                }
            row["count"] += int(item.get("count") or 1)
            row["agents"].append({"name": result["name"], "count": int(item.get("count") or 1)})
            # One member calling it major makes the whole row major — the fleet's worst
            # experience of a rough edge is the one worth acting on.
            if _SEVERITY_RANK.get(str(item.get("severity")), 0) > _SEVERITY_RANK.get(str(row["severity"]), 0):
                row["severity"] = item.get("severity")
            last, first = str(item.get("last_seen") or ""), str(item.get("first_seen") or "")
            row["last_seen"] = max(row["last_seen"], last)
            row["first_seen"] = min(row["first_seen"], first) if row["first_seen"] and first else (row["first_seen"] or first)
            if not row["detail"]:
                row["detail"] = item.get("detail", "")
    return {
        "items": sorted(merged.values(), key=_triage_rank, reverse=True),
        "members": reached,
        "unreachable": unreachable,
        "is_hub": True,
    }


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
    # Rule 2 (docs/guides/building-react-plugin-views.md): the data API belongs in the
    # plugin's own namespace. #2607 shipped it at the top-level /api/friction and that is
    # a documented, in-use path, so the canonical mount is added rather than swapped and
    # the old one is kept as an alias.
    registry.register_router(_build_router(), prefix="/api/plugins/friction")
    registry.register_router(_build_router(legacy=True), prefix="")
    # Default prefix (None) resolves to /plugins/friction — the canonical public
    # view prefix (ADR 0026) — so the manifest's views[].path matches exactly.
    registry.register_router(_build_view_router())
