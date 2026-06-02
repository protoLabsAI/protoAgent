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

from evals.compare import _category_passed, _pct  # noqa: E402

_RESULTS_DIR = Path(__file__).parent / "results"


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

    # Leaderboard: latest run per model, best overall first.
    latest = {m: reps[-1] for m, reps in by_model.items()}
    cats = sorted({c for rep in latest.values() for c in _category_passed(rep)})
    lines.append("## Leaderboard (latest run per model)")
    lines.append("")
    lines.append("| Model | " + " | ".join(cats) + " | **Overall** | Latency | Tokens | Runs |")
    lines.append("|" + "---|" * (len(cats) + 4))
    ordered = sorted(
        latest.items(),
        key=lambda kv: (kv[1].get("passed", 0) / kv[1].get("total", 1)) if kv[1].get("total") else 0,
        reverse=True,
    )
    for model, rep in ordered:
        cper = _category_passed(rep)
        cells = []
        for c in cats:
            pp, tt = cper.get(c, (0, 0))
            cells.append(f"{pp}/{tt}" if tt else "—")
        overall = f"**{rep.get('passed', 0)}/{rep.get('total', 0)} ({_pct(rep.get('passed', 0), rep.get('total', 0))})**"
        lines.append(
            f"| `{model}` | " + " | ".join(cells)
            + f" | {overall} | {_avg_latency(rep)}ms | {_avg_tokens(rep) or '—'} | {len(by_model[model])} |"
        )
    lines.append("")

    # Per-model trend over time.
    lines.append("## Trend (pass rate by run)")
    for model, reps in sorted(by_model.items()):
        lines.append("")
        lines.append(f"### `{model}`")
        lines.append("")
        lines.append("| When | Pass | Rate | Report |")
        lines.append("|---|---|---|---|")
        prev = None
        for rep in reps:
            p, t = rep.get("passed", 0), rep.get("total", 0)
            arrow = ""
            if prev is not None:
                d = p - prev
                arrow = " ▲" if d > 0 else (" ▼" if d < 0 else " ■")
            prev = p
            when = (rep.get("ts") or "")[:19].replace("T", " ")
            lines.append(f"| {when} | {p}/{t}{arrow} | {_pct(p, t)} | `{rep.get('_file', '?')}` |")
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
