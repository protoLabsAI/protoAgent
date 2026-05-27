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


def _by_id(report: dict) -> dict[str, bool]:
    return {r["id"]: bool(r["passed"]) for r in report.get("results", [])}


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
    old = json.loads(Path(argv[0]).read_text())
    new = json.loads(Path(argv[1]).read_text())
    print(compare_reports(old, new))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
