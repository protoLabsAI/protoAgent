"""Compare two eval runs into a markdown diff report.

Reads two reports written by ``evals.runner`` (``evals/results/run-*.json``,
shape ``{"total", "passed", "results": [{"id","category","name","passed",
"detail"}]}``) and reports the pass-rate delta, per-category deltas, and which
cases flipped — the regression check for a model/backend/prompt change.

Backported from the protoLabs fleet (protoResearcher ``evals/compare.py``).

    python -m evals.compare evals/results/run-OLD.json evals/results/run-NEW.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

# ADR 0126 D1 — eval tiers. ``gate`` is deterministic conformity ("this still
# works") and fails the nightly run; ``tracked`` is LLM-judge frequency ("how
# well, how often") and only ever moves a trend. Gate is the headline number,
# so it is always reported first.
_VALID_TIERS = ("gate", "tracked")
_TIER_ORDER = {t: i for i, t in enumerate(_VALID_TIERS)}


def resolve_tier(item: dict) -> str:
    """Resolve the eval tier of a case or a report row (ADR 0126 D1).

    An explicit ``tier`` wins and must be one of ``gate``/``tracked`` — an
    unknown value is a load-time error naming the case so a typo can't silently
    drop a case out of the gate. Otherwise a case/row carrying a ``verify_rubric``
    defaults to ``tracked`` (graded by the LLM judge, trend only) and everything
    else to ``gate`` (deterministic, counts against the nightly gate).

    The same resolver reads cases (which carry ``verify_rubric``) and historical
    report rows (which carry a resolved ``tier``, or neither when they predate
    this field — in which case they fall through to ``gate``)."""
    tier = item.get("tier")
    if tier is not None:
        if tier not in _VALID_TIERS:
            raise ValueError(
                f"case {item.get('id', '?')!r}: unknown tier {tier!r} "
                f"(expected one of {', '.join(_VALID_TIERS)})"
            )
        return tier
    return "tracked" if item.get("verify_rubric") else "gate"


def _tier_order(tier: str) -> tuple[int, str]:
    """Sort key putting ``gate`` before ``tracked`` before any other tier."""
    return (_TIER_ORDER.get(tier, len(_VALID_TIERS)), tier)


def _by_id(report: dict) -> dict[str, bool]:
    return {r["id"]: bool(r["passed"]) for r in report.get("results", [])}


def _tier_passed(report: dict) -> dict[str, tuple[int, int]]:
    """tier -> (passed, total), recomputed from rows (skipped rows excluded).

    Each row's tier is resolved with :func:`resolve_tier`, so legacy reports
    whose rows predate the ``tier`` field still split (defaulting to ``gate``)."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in report.get("results", []):
        if r.get("skipped"):
            continue
        cell = agg[resolve_tier(r)]
        cell[1] += 1
        if r.get("passed"):
            cell[0] += 1
    return {t: (p, tot) for t, (p, tot) in agg.items()}


def _tiers_of(report: dict) -> dict[str, tuple[int, int]]:
    """tier -> (passed, total). Prefers the report's own ``tiers`` block (written
    by the runner since ADR 0126 E1b); falls back to recomputing from rows so a
    legacy report with no tiers block / no per-row tier still renders a split."""
    block = report.get("tiers")
    if isinstance(block, dict) and block:
        out: dict[str, tuple[int, int]] = {}
        for tier, agg in block.items():
            if isinstance(agg, dict):
                out[tier] = (int(agg.get("passed", 0)), int(agg.get("total", 0)))
        if out:
            return out
    return _tier_passed(report)


def _category_passed(report: dict) -> dict[str, tuple[int, int]]:
    """category -> (passed, total)."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in report.get("results", []):
        cell = agg[r.get("category", "?")]
        cell[1] += 1
        if r.get("passed"):
            cell[0] += 1
    return {c: (p, t) for c, (p, t) in agg.items()}


def _pct(passed: int, total: int) -> str:
    return f"{(100 * passed / total):.0f}%" if total else "—"


def compare_reports(old: dict, new: dict) -> str:
    """Return a markdown comparison of two eval reports."""
    o_total, o_pass = old.get("total", 0), old.get("passed", 0)
    n_total, n_pass = new.get("total", 0), new.get("passed", 0)
    delta = n_pass - o_pass

    lines = ["# Eval comparison", ""]
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "■")
    lines.append(
        f"**Overall:** {o_pass}/{o_total} ({_pct(o_pass, o_total)}) → "
        f"{n_pass}/{n_total} ({_pct(n_pass, n_total)})  {arrow} {delta:+d}"
    )
    lines.append("")

    # Per-tier — gate first, since the gate pass rate is the number that gates
    # the nightly run (ADR 0126 D1). Regressions and improvements are reported
    # per tier so a tracked-tier judge wobble is never read as a gate break.
    ot, nt_ = _tiers_of(old), _tiers_of(new)
    tiers = sorted(set(ot) | set(nt_), key=_tier_order)
    lines.append("| Tier | Old | New | Δ |")
    lines.append("|---|---|---|---|")
    for tier in tiers:
        op, otot = ot.get(tier, (0, 0))
        nps, ntot = nt_.get(tier, (0, 0))
        label = f"**{tier}**" if tier == "gate" else tier
        lines.append(f"| {label} | {op}/{otot} | {nps}/{ntot} | {nps - op:+d} |")
    lines.append("")

    # Per-category
    oc, nc = _category_passed(old), _category_passed(new)
    cats = sorted(set(oc) | set(nc))
    lines.append("| Category | Old | New | Δ |")
    lines.append("|---|---|---|---|")
    for c in cats:
        op, ot = oc.get(c, (0, 0))
        nps, nt = nc.get(c, (0, 0))
        lines.append(f"| {c} | {op}/{ot} | {nps}/{nt} | {nps - op:+d} |")
    lines.append("")

    # Flips
    o_ids, n_ids = _by_id(old), _by_id(new)
    regressed = sorted(i for i in n_ids if o_ids.get(i) and not n_ids[i])
    fixed = sorted(i for i in n_ids if n_ids[i] and o_ids.get(i) is False)
    if regressed:
        lines.append("### ❌ Newly failing")
        lines += [f"- `{i}`" for i in regressed]
        lines.append("")
    if fixed:
        lines.append("### ✅ Newly passing")
        lines += [f"- `{i}`" for i in fixed]
        lines.append("")
    if not regressed and not fixed:
        lines.append("_No cases flipped._")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        sys.stderr.write("usage: python -m evals.compare <old.json> <new.json>\n")
        return 2
    old = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    new = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    print(compare_reports(old, new))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
