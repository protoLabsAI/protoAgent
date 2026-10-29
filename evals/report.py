"""Eval trend report — track agent quality across runs and models.

Aggregates every model-tagged report in ``evals/results/`` into:

- a **leaderboard** of the latest standing per model (overall + per-category
  pass rate, avg latency/tokens), best first; and
- a **trend** per model — pass rate of each run over time, so a regression
  after a prompt/model/code change is visible at a glance.

Reports are keyed by the ``model`` field that ``evals.runner`` now stamps on
every report (auto-detected from ``/healthz``). Runs with no model tag are
grouped under ``(untagged)``.

    python -m evals.report                 # all results/
    python -m evals.report --dir some/dir
    python -m evals.report --model protolabs/reasoning   # one model's trend
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from evals.compare import (  # noqa: E402
    _category_passed,
    _pct,
    _tier_order,
    _tiers_of,
    is_instance_case,
    resolve_tier,
)

_RESULTS_DIR = Path(__file__).parent / "results"


def _tier_cell(tiers: dict[str, tuple[int, int]], tier: str) -> str:
    """``p/t (pct)`` for a tier, or ``—`` when the run had no cases in it."""
    p, t = tiers.get(tier, (0, 0))
    return f"{p}/{t} ({_pct(p, t)})" if t else "—"


def _gate_rate(rep: dict) -> float:
    """Gate pass rate (the headline number, ADR 0126 D1); 0.0 when a run has no
    gate cases so it sorts last rather than crashing."""
    p, t = _tiers_of(rep).get("gate", (0, 0))
    return p / t if t else 0.0


def _load_runs(results_dir: Path) -> list[dict]:
    """Single-run reports (``run-*.json``), oldest first by ``ts``.

    Skips ``sweep-*.json`` (combined) — their per-model reports are written
    separately as ``run-sweep-*`` and picked up here individually."""
    runs: list[dict] = []
    for path in sorted(results_dir.glob("run-*.json")):
        try:
            rep = json.loads(path.read_text())
        except Exception:
            continue
        if "results" not in rep:
            continue
        rep["_file"] = path.name
        runs.append(rep)
    runs.sort(key=lambda r: r.get("ts", ""))
    return runs


def _avg_latency(rep: dict) -> int:
    timed = [r for r in rep.get("results", []) if r.get("duration_ms")]
    return round(sum(r["duration_ms"] for r in timed) / len(timed)) if timed else 0


def _avg_tokens(rep: dict) -> int:
    toks = [r for r in rep.get("results", []) if r.get("tokens")]
    return round(sum(r["tokens"] for r in toks) / len(toks)) if toks else 0


def _instance_tiers(rep: dict) -> dict[str, tuple[int, int]]:
    """Per-tier ``(passed, total)`` over this run's INSTANCE cases only (ADR 0126
    D5). Skipped rows carry no pass/fail signal and are excluded, same as the core
    tier split; each row's tier is resolved with :func:`resolve_tier` so a legacy
    instance row with no stamped tier still lands in gate."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rep.get("results", []):
        if r.get("skipped") or not is_instance_case(r):
            continue
        cell = agg[resolve_tier(r)]
        cell[1] += 1
        if r.get("passed"):
            cell[0] += 1
    return {t: (p, tot) for t, (p, tot) in agg.items()}


def _instance_section(latest: dict[str, dict]) -> list[str]:
    """The ``## Instance cases`` block (ADR 0126 D5): the latest run per model that
    actually ran instance cases, their pass rate split by tier and kept apart from
    the core suite. Empty list when no run has any instance case, so a plain suite's
    report is unchanged."""
    with_instance = {m: _instance_tiers(rep) for m, rep in latest.items()}
    with_instance = {m: t for m, t in with_instance.items() if t}
    if not with_instance:
        return []
    tiers = sorted({t for cells in with_instance.values() for t in cells}, key=_tier_order)
    lines = [
        "",
        "## Instance cases",
        "",
        "Cases loaded from `<config dir>/evals/cases.json` (namespaced `instance:*`, "
        "ADR 0126 D5) — latest run per model, split by tier.",
        "",
        "| Model | " + " | ".join(t.capitalize() for t in tiers) + " | Overall |",
        "|" + "---|" * (len(tiers) + 2),
    ]
    for model in sorted(with_instance):
        cells_by_tier = with_instance[model]
        cells = []
        tot_p = tot_t = 0
        for tier in tiers:
            p, t = cells_by_tier.get(tier, (0, 0))
            tot_p += p
            tot_t += t
            cells.append(f"{p}/{t} ({_pct(p, t)})" if t else "—")
        overall = f"{tot_p}/{tot_t} ({_pct(tot_p, tot_t)})"
        lines.append(f"| `{model}` | " + " | ".join(cells) + f" | {overall} |")
    lines.append("")
    return lines


def build_report(runs: list[dict], only_model: str | None = None) -> str:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for rep in runs:
        model = rep.get("model") or "(untagged)"
        if only_model and model != only_model:
            continue
        by_model[model].append(rep)

    if not by_model:
        return "_No model-tagged eval runs found._"

    lines: list[str] = ["# Eval trend report", ""]

    # Leaderboard: latest run per model, best GATE pass rate first — the gate is
    # the headline number (ADR 0126 D1), so the board ranks and bolds it. Gate
    # and tracked are shown apart; overall is kept alongside for continuity.
    latest = {m: reps[-1] for m, reps in by_model.items()}
    cats = sorted({c for rep in latest.values() for c in _category_passed(rep)})
    lines.append("## Leaderboard (latest run per model)")
    lines.append("")
    lines.append(
        "| Model | " + " | ".join(cats) + " | **Gate** | Tracked | Overall | Latency | Tokens | Runs |"
    )
    # Columns: Model + categories + Gate + Tracked + Overall + Latency + Tokens + Runs.
    lines.append("|" + "---|" * (len(cats) + 7))
    ordered = sorted(latest.items(), key=lambda kv: _gate_rate(kv[1]), reverse=True)
    for model, rep in ordered:
        cper = _category_passed(rep)
        cells = []
        for c in cats:
            pp, tt = cper.get(c, (0, 0))
            cells.append(f"{pp}/{tt}" if tt else "—")
        tiers = _tiers_of(rep)
        gp, gt = tiers.get("gate", (0, 0))
        gate = f"**{gp}/{gt} ({_pct(gp, gt)})**" if gt else "—"
        tracked = _tier_cell(tiers, "tracked")
        overall = f"{rep.get('passed', 0)}/{rep.get('total', 0)} ({_pct(rep.get('passed', 0), rep.get('total', 0))})"
        lines.append(
            f"| `{model}` | "
            + " | ".join(cells)
            + f" | {gate} | {tracked} | {overall} | {_avg_latency(rep)}ms | {_avg_tokens(rep) or '—'} | {len(by_model[model])} |"
        )
    lines.append("")

    # Per-model trend over time — gate and tracked tracked apart, gate first and
    # carrying the delta arrow since it is the number that gates the run.
    lines.append("## Trend (pass rate by run)")
    for model, reps in sorted(by_model.items()):
        lines.append("")
        lines.append(f"### `{model}`")
        lines.append("")
        lines.append("| When | Gate | Tracked | Overall | Report |")
        lines.append("|---|---|---|---|---|")
        prev = None
        for rep in reps:
            tiers = _tiers_of(rep)
            gp, gt = tiers.get("gate", (0, 0))
            arrow = ""
            if prev is not None:
                d = gp - prev
                arrow = " ▲" if d > 0 else (" ▼" if d < 0 else " ■")
            prev = gp
            gate = f"{gp}/{gt} ({_pct(gp, gt)}){arrow}" if gt else f"—{arrow}"
            tracked = _tier_cell(tiers, "tracked")
            p, t = rep.get("passed", 0), rep.get("total", 0)
            overall = f"{p}/{t} ({_pct(p, t)})"
            when = (rep.get("ts") or "")[:19].replace("T", " ")
            lines.append(f"| {when} | {gate} | {tracked} | {overall} | `{rep.get('_file', '?')}` |")

    # Per-instance cases under their own heading, still split by tier (ADR 0126 D5)
    # — a domain suite's pass rate kept distinct from the core suite's. Appended
    # only when some run actually carried instance cases, so a plain report is
    # byte-for-byte unchanged.
    lines += _instance_section(latest)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Aggregate eval results into a leaderboard + trend.")
    p.add_argument("--dir", default=str(_RESULTS_DIR), help="results directory")
    p.add_argument("--model", default=None, help="restrict the trend to one model")
    args = p.parse_args(argv)

    runs = _load_runs(Path(args.dir))
    if not runs:
        sys.stderr.write(f"no run-*.json reports found in {args.dir}\n")
        return 1
    print(build_report(runs, only_model=args.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
