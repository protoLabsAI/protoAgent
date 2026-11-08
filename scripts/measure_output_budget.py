#!/usr/bin/env python3
"""Reproduce the ADR 0128 output-budget audit from per-call telemetry (#4875 S0).

Host-free: standard library only, no imports from the protoAgent tree, so it runs
on an analyst's laptop against an exported JSONL with nothing installed. It MEASURES
and changes no behaviour — S0's whole job is to size the problem ADR 0128 fixes.

The manager runs it on the real 2026-10-30 → 2026-11-08 window after merge and posts
the numbers on #4875; the fixtures in ``tests/test_output_budget_measure.py`` are the
only inputs exercised here.

INPUT
-----
A JSONL file (one JSON object per line) of PER-CALL rows — one row per model reply.
``--source`` selects which shape the rows are in; both normalise to the same internal
call record and every metric is computed on that.

``--source ledger`` — protoAgent-native rows, the shape ``_extract_subagent_usage``
(``src/graph/agent.py``, #4875 S0) stamps on each reply and the delegation-ledger /
turns telemetry pipeline carries:

    key                field used                                       default
    output_tokens      output_tokens                                    0
    finish_reason      finish_reason                                    None
    text_chars         text_chars (visible chars, reasoning excluded)   None
    has_tool_calls     has_tool_calls                                   False
    reasoning_tokens   reasoning_tokens                                 None
    model              model                                            ""
    agent / subagent   from_agent / subagent_type  (tags, informational)
    timestamp          at | timestamp | ended_at   (ISO-8601, for the window)
    retried            retried   (the completion guard fired a retry)   False
    recovered          recovered (that retry produced a usable reply)   False

``--source langfuse`` — a Langfuse generations export, one generation (= one model
call) per line, tagged by agent and subagent in ``metadata``:

    key                read from                                                 default
    output_tokens      usageDetails.output | usage.output | usage.output_tokens
                       | usage.completion_tokens                                  0
    reasoning_tokens   usageDetails.reasoning
                       | usage.output_token_details.reasoning
                       | metadata.reasoning_tokens                                None
    finish_reason      metadata.finish_reason | finish_reason
                       | metadata.stop_reason                                     None
    has_tool_calls     metadata.has_tool_calls                                    False
    text_chars         metadata.text_chars, else len(str(output)) when output
                       is a string                                               None
    model              model | metadata.model                                    ""
    agent / subagent   metadata.agent / metadata.subagent
    timestamp          startTime | timestamp | endTime                           None
    retried/recovered  metadata.retried / metadata.recovered                     False

``--panels`` (optional) — pr-reviewer telemetry JSONL, one panel per line, carrying a
``step_s`` field for finder-stage wall time. ``step_s`` may be a mapping of step name →
seconds (``{"find_correctness": 120.5, ...}``) or a single number for a one-step record
with its name in ``step``. Finder steps are those whose name contains ``find``
(``find_correctness`` / ``find_removed_behavior`` / ``find_crossfile`` /
``find_conventions``). Panel count is the number of panel records.

``--log`` (optional) — a plain-text protoAgent log. The recovered-after-retry count can
come from each row's ``recovered`` flag; absent that, this log is scanned for the
completion-guard's reasoning-only retry line (``graph/middleware/completion_guard.py``)
minus its give-up line.

OUTPUT
------
Text by default; add ``--json`` for a machine-readable object. Either way it reports:
the share of calls over 8k output tokens and those calls' share of all output tokens;
cap hits (``finish_reason`` in {length, max_tokens}); empty calls (visible text ≤ 200
and no tool calls) as N of M; finder-stage hours, panel count and hours per panel; the
split by turn kind (tool-call vs run-ending) of the over-8k calls and of the empty
calls; and how many empty calls recovered after a reasoning-only retry.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

#: A call emitting more than this many output tokens is "large" (ADR 0128 finding F6:
#: 296 of 20,225 calls, 1.5%, were 25% of all output tokens).
OVER_THRESHOLD = 8000
#: Visible-text ceiling for an "empty" call (ADR 0128 D4 / completion_guard.py).
EMPTY_TEXT_MAX = 200
#: finish_reason values that mean the reply was cut at the output cap.
CAP_REASONS = frozenset({"length", "max_tokens"})
#: The completion guard's reasoning-only retry line and its give-up line
#: (graph/middleware/completion_guard.py) — the --log recovery heuristic.
LOG_RETRY_MARK = "reasoning-only turn; retrying once"
LOG_GIVEUP_MARK = "letting the run end"


# --------------------------------------------------------------------------- #
# Normalisation: both source shapes -> a common call record.
# --------------------------------------------------------------------------- #
def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _opt_int(value):
    """int(value), or None when the field is absent/unparseable (a missing value is None)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_path(obj: dict, *path):
    """Nested ``obj[a][b]...`` or None if any hop is missing / not a dict."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def normalize_ledger(row: dict) -> dict:
    """A protoAgent-native per-call row -> the common call record."""
    return {
        "output_tokens": _as_int(row.get("output_tokens")),
        "reasoning_tokens": _opt_int(row.get("reasoning_tokens")),
        "finish_reason": _norm_reason(row.get("finish_reason")),
        "has_tool_calls": bool(row.get("has_tool_calls")),
        "text_chars": _opt_int(row.get("text_chars")),
        "model": str(row.get("model") or ""),
        "agent": str(row.get("from_agent") or row.get("agent") or ""),
        "subagent": str(row.get("subagent_type") or row.get("subagent") or ""),
        "timestamp": row.get("at") or row.get("timestamp") or row.get("ended_at"),
        "retried": bool(row.get("retried")),
        "recovered": bool(row.get("recovered")),
    }


def normalize_langfuse(gen: dict) -> dict:
    """A Langfuse generation export object -> the common call record."""
    meta = gen.get("metadata") if isinstance(gen.get("metadata"), dict) else {}
    output_tokens = (
        _get_path(gen, "usageDetails", "output")
        or _get_path(gen, "usage", "output")
        or _get_path(gen, "usage", "output_tokens")
        or _get_path(gen, "usage", "completion_tokens")
    )
    reasoning = (
        _get_path(gen, "usageDetails", "reasoning")
        or _get_path(gen, "usage", "output_token_details", "reasoning")
        or meta.get("reasoning_tokens")
    )
    text_chars = meta.get("text_chars")
    if text_chars is None and isinstance(gen.get("output"), str):
        text_chars = len(gen["output"])
    return {
        "output_tokens": _as_int(output_tokens),
        "reasoning_tokens": _opt_int(reasoning),
        "finish_reason": _norm_reason(meta.get("finish_reason") or gen.get("finish_reason") or meta.get("stop_reason")),
        "has_tool_calls": bool(meta.get("has_tool_calls")),
        "text_chars": _opt_int(text_chars),
        "model": str(gen.get("model") or meta.get("model") or ""),
        "agent": str(meta.get("agent") or ""),
        "subagent": str(meta.get("subagent") or ""),
        "timestamp": gen.get("startTime") or gen.get("timestamp") or gen.get("endTime"),
        "retried": bool(meta.get("retried")),
        "recovered": bool(meta.get("recovered")),
    }


_NORMALIZERS = {"ledger": normalize_ledger, "langfuse": normalize_langfuse}


def _norm_reason(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


# --------------------------------------------------------------------------- #
# Reading + window filtering.
# --------------------------------------------------------------------------- #
def _parse_dt(value):
    """Lenient ISO-8601 parse; a trailing ``Z`` becomes ``+00:00``. None on failure."""
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def iter_jsonl(text: str):
    """Yield the parsed object of each non-blank line of a JSONL string."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)


def in_window(timestamp, since, until) -> bool:
    """Whether ``timestamp`` falls in [since, until]. A row with no parseable timestamp
    is kept only when neither bound is set (an active window drops what it can't place)."""
    if since is None and until is None:
        return True
    moment = _parse_dt(timestamp)
    if moment is None:
        return False
    if since is not None and moment < since:
        return False
    if until is not None and moment > until:
        return False
    return True


def load_calls(text: str, source: str, since=None, until=None):
    """Parse a JSONL string into normalised call records inside the window.

    Returns ``(calls, dropped_outside_window)``."""
    normalize = _NORMALIZERS[source]
    calls, dropped = [], 0
    for obj in iter_jsonl(text):
        call = normalize(obj)
        if in_window(call.get("timestamp"), since, until):
            calls.append(call)
        else:
            dropped += 1
    return calls, dropped


# --------------------------------------------------------------------------- #
# The metrics.
# --------------------------------------------------------------------------- #
def _turn_kind(call: dict) -> str:
    """A reply that calls a tool continues the run; one that doesn't ends it."""
    return "tool_call" if call.get("has_tool_calls") else "run_ending"


def _split_by_turn_kind(calls) -> dict:
    split = {"tool_call": 0, "run_ending": 0}
    for call in calls:
        split[_turn_kind(call)] += 1
    return split


def is_over(call: dict) -> bool:
    return _as_int(call.get("output_tokens")) > OVER_THRESHOLD


def is_cap_hit(call: dict) -> bool:
    return call.get("finish_reason") in CAP_REASONS


def is_empty(call: dict) -> bool:
    """No tool calls and visible text at or below the ceiling. An unknown
    ``text_chars`` (None) cannot be confirmed empty, so it is not counted."""
    chars = call.get("text_chars")
    return (not call.get("has_tool_calls")) and chars is not None and chars <= EMPTY_TEXT_MAX


def scan_log_recovery(text: str) -> dict:
    """Completion-guard retry accounting from a raw log: reasoning-only retries, give-ups,
    and ``recovered = max(0, retries - give-ups)``."""
    retries = sum(1 for line in text.splitlines() if LOG_RETRY_MARK in line)
    gave_up = sum(1 for line in text.splitlines() if "reasoning-only turn" in line and LOG_GIVEUP_MARK in line)
    return {"retries": retries, "gave_up": gave_up, "recovered": max(0, retries - gave_up)}


def summarize_panels(text: str) -> dict:
    """Finder-stage wall time from a pr-reviewer panels JSONL."""
    finder_seconds = 0.0
    panels = 0
    for obj in iter_jsonl(text):
        panels += 1
        step_s = obj.get("step_s")
        if isinstance(step_s, dict):
            for name, secs in step_s.items():
                if "find" in str(name).lower():
                    finder_seconds += float(secs or 0)
        elif step_s is not None:
            name = str(obj.get("step") or "")
            if "find" in name.lower():
                finder_seconds += float(step_s or 0)
    hours = finder_seconds / 3600.0
    return {
        "panels": panels,
        "finder_hours": round(hours, 4),
        "hours_per_panel": round(hours / panels, 4) if panels else 0.0,
    }


def _share(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def analyze(calls, *, panels=None, log_text=None) -> dict:
    """The full ADR 0128 S0 report as a plain dict."""
    total_calls = len(calls)
    total_output = sum(_as_int(c.get("output_tokens")) for c in calls)

    over = [c for c in calls if is_over(c)]
    over_output = sum(_as_int(c.get("output_tokens")) for c in over)
    empty = [c for c in calls if is_empty(c)]

    recovered_rows = sum(1 for c in empty if c.get("recovered"))
    retried_rows = sum(1 for c in empty if c.get("retried") or c.get("recovered"))
    log_recovery = scan_log_recovery(log_text) if log_text is not None else None
    # Rows are authoritative when any carry a recovered flag; else fall back to the log.
    recovered = recovered_rows if recovered_rows else (log_recovery["recovered"] if log_recovery else 0)

    report = {
        "calls": total_calls,
        "output_tokens_total": total_output,
        "over_8k": {
            "threshold": OVER_THRESHOLD,
            "calls": len(over),
            "call_share": round(_share(len(over), total_calls), 6),
            "output_tokens": over_output,
            "token_share": round(_share(over_output, total_output), 6),
            "by_turn_kind": _split_by_turn_kind(over),
        },
        "cap_hits": sum(1 for c in calls if is_cap_hit(c)),
        "empty": {
            "calls": len(empty),
            "of": total_calls,
            "by_turn_kind": _split_by_turn_kind(empty),
            "retried": retried_rows,
            "recovered": recovered,
        },
    }
    if log_recovery is not None:
        report["empty"]["log_recovery"] = log_recovery
    report["finder"] = summarize_panels(panels) if panels is not None else None
    return report


# --------------------------------------------------------------------------- #
# Rendering + CLI.
# --------------------------------------------------------------------------- #
def _pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def render_text(report: dict) -> str:
    over = report["over_8k"]
    empty = report["empty"]
    lines = [
        f"Calls measured: {report['calls']}",
        f"Output tokens total: {report['output_tokens_total']}",
        "",
        f"Over {over['threshold']} output tokens: {over['calls']} of {report['calls']} "
        f"({_pct(over['call_share'])} of calls), {over['output_tokens']} tokens "
        f"({_pct(over['token_share'])} of all output tokens)",
        f"  by turn kind: tool-call {over['by_turn_kind']['tool_call']}, "
        f"run-ending {over['by_turn_kind']['run_ending']}",
        f"Cap hits (finish_reason length/max_tokens): {report['cap_hits']}",
        f"Empty calls (<= {EMPTY_TEXT_MAX} visible chars, no tool calls): {empty['calls']} of {empty['of']}",
        f"  by turn kind: tool-call {empty['by_turn_kind']['tool_call']}, "
        f"run-ending {empty['by_turn_kind']['run_ending']}",
        f"  recovered after a reasoning-only retry: {empty['recovered']} (retried {empty['retried']})",
    ]
    finder = report.get("finder")
    if finder is not None:
        lines.append(
            f"Finder stage: {finder['finder_hours']} hours across {finder['panels']} panels "
            f"({finder['hours_per_panel']} hours/panel)"
        )
    return "\n".join(lines)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure the ADR 0128 output budget from per-call telemetry.")
    parser.add_argument("rows", help="JSONL of per-call rows (see --source for the shape).")
    parser.add_argument("--source", choices=sorted(_NORMALIZERS), required=True, help="Input row shape.")
    parser.add_argument("--panels", help="Optional pr-reviewer telemetry JSONL (step_s) for finder-stage wall time.")
    parser.add_argument("--log", help="Optional plain-text log for the recovered-after-retry count.")
    parser.add_argument("--since", help="Keep calls at or after this ISO-8601 instant.")
    parser.add_argument("--until", help="Keep calls at or before this ISO-8601 instant.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON instead of text.")
    return parser


def run(argv=None) -> int:
    args = build_parser().parse_args(argv)
    since = _parse_dt(args.since) if args.since else None
    until = _parse_dt(args.until) if args.until else None
    if args.since and since is None:
        print(f"error: --since is not an ISO-8601 instant: {args.since!r}", file=sys.stderr)
        return 2
    if args.until and until is None:
        print(f"error: --until is not an ISO-8601 instant: {args.until!r}", file=sys.stderr)
        return 2

    calls, dropped = load_calls(_read(args.rows), args.source, since=since, until=until)
    report = analyze(
        calls,
        panels=_read(args.panels) if args.panels else None,
        log_text=_read(args.log) if args.log else None,
    )
    if dropped:
        report["dropped_outside_window"] = dropped

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
        if dropped:
            print(f"(dropped {dropped} rows outside the --since/--until window)")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(run())
