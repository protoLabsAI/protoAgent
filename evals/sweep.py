"""Model-comparison eval sweep.

Boots a throwaway, UI-less agent per model, runs the eval suite against
it, tags the report with the model, tears the agent down, and prints a
``model × category`` pass-rate matrix — the one-command way to answer
"which model is best for this agent?" and to catch a regression when you
swap the default.

How it works (per model):

1. Launch ``python -m server --port <p> --ui none`` with ``PROTOAGENT_MODEL=<model>``
   (the env override added in ``graph/config.py``) and a unique
   ``PROTOAGENT_INSTANCE`` so the models never share scoped data.
2. Wait for ``GET /healthz`` to report the graph compiled.
3. Run ``python -m evals.runner`` against that base URL, tagged with the model.
4. Terminate the agent and delete its instance data.

Usage::

    python -m evals.sweep --models protolabs/reasoning,protolabs/smart
    python -m evals.sweep --models a,b,c --category tool
    python -m evals.sweep --models a,b --tasks current_time,memory_ingest --keep
    python -m evals.sweep --models a,b,c --category tool --repeat 3   # best-of-3

``--repeat N`` runs the suite N times per model (against the same booted agent)
and prints a per-case ``passes/N`` table, scoring each model on the cases that
passed the majority of runs — the way to see past single-run sampling noise on
non-deterministic cases (tool selection especially).

The combined result lands in ``evals/results/sweep-<ts>.json``; each run is
written alongside as ``run-sweep-<ts>-<model>[-r<i>].json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals.compare import _category_passed, _pct  # noqa: E402

_RESULTS_DIR = Path(__file__).parent / "results"
_HEALTH_DEADLINE_S = 90.0
_HEALTH_INTERVAL_S = 1.0


def _slug(model: str) -> str:
    """Filesystem-safe token for a model alias (``protolabs/reasoning`` →
    ``protolabs-reasoning``)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-")


def _instance_dirs(instance: str) -> list[Path]:
    """The scoped-data dirs an instance creates under ``~/.protoagent``."""
    base = Path(os.path.expanduser("~/.protoagent"))
    return [
        base / instance,
        base / "inbox" / instance,
        base / "scheduler" / instance,
        base / "knowledge" / instance,
    ]


def _cleanup_instance(instance: str) -> None:
    for p in _instance_dirs(instance):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)


def _wait_healthy(base_url: str, deadline_s: float = _HEALTH_DEADLINE_S) -> dict | None:
    """Poll ``/healthz`` until the graph is compiled (200). Returns the health
    body, or None if it never came up."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=2)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(_HEALTH_INTERVAL_S)
    return None


def _run_one_model(
    model: str,
    *,
    port: int,
    instance: str,
    ts: int,
    category: str | None,
    tasks: str | None,
    keep: bool,
    repeat: int = 1,
) -> list[dict]:
    """Boot one agent on ``model`` and run the suite ``repeat`` times against
    it; return the list of report dicts (empty if the agent never came up).

    Repeats run against the *same* booted agent on purpose — that isolates the
    model's own run-to-run sampling variance (the thing best-of-N measures) from
    boot/cold-start variance, and costs one boot per model instead of N."""
    base_url = f"http://127.0.0.1:{port}"
    log_path = _RESULTS_DIR / f"server-sweep-{ts}-{_slug(model)}.log"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "PROTOAGENT_MODEL": model,
        "PROTOAGENT_INSTANCE": instance,
        "PROTOAGENT_UI": "none",
    }
    # Give the throwaway agent a bearer token so the auth-gating eval cases are
    # actually exercised (an unconfigured instance accepts any token → the
    # negative-auth case can't pass). The runner's client reads the same env
    # var, so the good-token cases still authenticate. Respect a token the
    # operator already set.
    env.setdefault("A2A_AUTH_TOKEN", f"eval-sweep-{ts}")
    print(f"\n=== {model} :: booting on {base_url} (instance={instance}) ===")
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server", "--port", str(port), "--ui", "none"],
        cwd=str(_PROJECT_ROOT),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    try:
        health = _wait_healthy(base_url)
        if health is None:
            print(f"  ✗ agent never became healthy (see {log_path})")
            return None
        print(f"  ✓ healthy (model={health.get('model')})")

        reports: list[dict] = []
        for run_i in range(repeat):
            suffix = f"-r{run_i + 1}" if repeat > 1 else ""
            report_path = _RESULTS_DIR / f"run-sweep-{ts}-{_slug(model)}{suffix}.json"
            cmd = [
                sys.executable, "-m", "evals.runner",
                "--base-url", base_url,
                "--model-label", model,
                "--out", str(report_path),
            ]
            if category:
                cmd += ["--category", category]
            if tasks:
                cmd += ["--tasks", tasks]
            if repeat > 1:
                print(f"  — run {run_i + 1}/{repeat}")
            subprocess.run(cmd, cwd=str(_PROJECT_ROOT), env=env, check=False)
            if report_path.exists():
                reports.append(json.loads(report_path.read_text()))
            else:
                print(f"  ✗ no report written for {model} (run {run_i + 1})")
        return reports
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_f.close()
        if not keep:
            _cleanup_instance(instance)


def _render_matrix(reports: dict[str, dict]) -> str:
    """A ``model × category`` pass-rate matrix + an overall leaderboard."""
    cats = sorted({c for rep in reports.values() for c in _category_passed(rep)})
    lines = ["# Model sweep", ""]

    # Per-category matrix.
    header = "| Model | " + " | ".join(cats) + " | **Overall** |"
    lines.append(header)
    lines.append("|" + "---|" * (len(cats) + 2))
    # Sort the leaderboard by overall pass rate, best first.
    ordered = sorted(
        reports.items(),
        key=lambda kv: (kv[1].get("passed", 0) / kv[1].get("total", 1)) if kv[1].get("total") else 0,
        reverse=True,
    )
    for model, rep in ordered:
        cper = _category_passed(rep)
        cells = []
        for c in cats:
            p, t = cper.get(c, (0, 0))
            cells.append(f"{p}/{t} ({_pct(p, t)})" if t else "—")
        overall = f"**{rep.get('passed', 0)}/{rep.get('total', 0)} ({_pct(rep.get('passed', 0), rep.get('total', 0))})**"
        lines.append(f"| `{model}` | " + " | ".join(cells) + f" | {overall} |")
    lines.append("")

    # Cost/latency footnote (avg per case across the suite).
    lines.append("| Model | Avg latency | Avg tokens |")
    lines.append("|---|---|---|")
    for model, rep in ordered:
        rs = rep.get("results", [])
        timed = [r for r in rs if r.get("duration_ms")]
        toks = [r for r in rs if r.get("tokens")]
        avg_ms = round(sum(r["duration_ms"] for r in timed) / len(timed)) if timed else 0
        avg_t = round(sum(r["tokens"] for r in toks) / len(toks)) if toks else 0
        lines.append(f"| `{model}` | {avg_ms}ms | {avg_t or '—'} |")
    return "\n".join(lines)


def _majority(repeat: int) -> int:
    """Best-of-N threshold: a case passes when it passed the majority of runs."""
    return repeat // 2 + 1


def _aggregate_runs(runs: list[dict]) -> dict[str, tuple[int, int]]:
    """Across a model's N runs → case_id -> (passes, n_runs_seen)."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for rep in runs:
        for r in rep.get("results", []):
            cell = agg[r["id"]]
            cell[1] += 1
            if r.get("passed"):
                cell[0] += 1
    return {cid: (p, n) for cid, (p, n) in agg.items()}


def _avg_latency(runs: list[dict]) -> int:
    timed = [r["duration_ms"] for rep in runs for r in rep.get("results", []) if r.get("duration_ms")]
    return round(sum(timed) / len(timed)) if timed else 0


def _render_repeat_matrix(model_runs: dict[str, list[dict]], repeat: int) -> str:
    """A per-case best-of-N table: each cell is ``passes/N``; a model's
    best-of-N score counts cases that passed the majority of runs."""
    threshold = _majority(repeat)
    agg = {m: _aggregate_runs(runs) for m, runs in model_runs.items()}
    cases = sorted({c for a in agg.values() for c in a})

    def best_of_n(m: str) -> int:
        return sum(1 for c, (p, _n) in agg[m].items() if p >= threshold)

    ordered = sorted(model_runs, key=best_of_n, reverse=True)
    short = {m: m.split("/")[-1] for m in ordered}

    lines = [
        f"# Model sweep — best-of-{repeat} (majority = {threshold}/{repeat})",
        "",
        "Each cell is `passes/runs`; ✗ marks a case that failed the majority of runs.",
        "",
        "| Case | " + " | ".join(short[m] for m in ordered) + " |",
        "|" + "---|" * (len(ordered) + 1),
    ]
    for c in cases:
        row = [f"`{c}`"]
        for m in ordered:
            p, n = agg[m].get(c, (0, 0))
            mark = "" if (n and p >= threshold) else " ✗"
            row.append(f"{p}/{n}{mark}" if n else "—")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("|" + "---|" * (len(ordered) + 1))
    total = len(cases)
    lines.append(
        "| **Best-of-N passed** | "
        + " | ".join(f"**{best_of_n(m)}/{total}**" for m in ordered) + " |"
    )
    lines.append(
        "| Avg latency | "
        + " | ".join(f"{_avg_latency(model_runs[m])}ms" for m in ordered) + " |"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the eval suite across multiple models.")
    p.add_argument("--models", required=True, help="comma-separated model aliases")
    p.add_argument("--category", default=None, help="restrict to one eval category")
    p.add_argument("--tasks", default=None, help="comma-separated case IDs")
    p.add_argument("--port-base", type=int, default=7990, help="first port (each model uses port-base+i)")
    p.add_argument("--keep", action="store_true", help="keep each model's instance data + logs")
    p.add_argument(
        "--repeat", type=int, default=1,
        help="run the suite N times per model for a best-of-N (majority) per-case table",
    )
    args = p.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        sys.stderr.write("no models given\n")
        return 2
    repeat = max(1, args.repeat)

    ts = int(time.time())
    model_runs: dict[str, list[dict]] = {}
    for i, model in enumerate(models):
        runs = _run_one_model(
            model,
            port=args.port_base + i,
            instance=f"eval-sweep-{ts}-{i}",
            ts=ts,
            category=args.category,
            tasks=args.tasks,
            keep=args.keep,
            repeat=repeat,
        )
        if runs:
            model_runs[model] = runs

    if not model_runs:
        sys.stderr.write("no model produced a report\n")
        return 1

    if repeat > 1:
        matrix = _render_repeat_matrix(model_runs, repeat)
    else:
        matrix = _render_matrix({m: runs[0] for m, runs in model_runs.items()})
    print("\n" + matrix)

    combined = _RESULTS_DIR / f"sweep-{ts}.json"
    combined.write_text(json.dumps({
        "ts": ts,
        "models": models,
        "repeat": repeat,
        # repeat>1: all N runs per model; repeat==1: the single run (list of one).
        "runs": model_runs,
    }, indent=2))
    (_RESULTS_DIR / f"sweep-{ts}.md").write_text(matrix + "\n")
    print(f"\nSweep: {combined}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
