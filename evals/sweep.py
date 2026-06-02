"""Model-comparison eval sweep.

Boots a throwaway, UI-less agent per model, runs the eval suite against
it, tags the report with the model, tears the agent down, and prints a
``model × category`` pass-rate matrix — the one-command way to answer
"which model is best for this agent?" and to catch a regression when you
swap the default.

How it works (per model):

1. Launch ``server.py --port <p> --ui none`` with ``PROTOAGENT_MODEL=<model>``
   (the env override added in ``graph/config.py``) and a unique
   ``PROTOAGENT_INSTANCE`` so the models never share scoped data.
2. Wait for ``GET /healthz`` to report the graph compiled.
3. Run ``python -m evals.runner`` against that base URL, tagged with the model.
4. Terminate the agent and delete its instance data.

Usage::

    python -m evals.sweep --models protolabs/reasoning,protolabs/smart
    python -m evals.sweep --models a,b,c --category tool
    python -m evals.sweep --models a,b --tasks current_time,daily_log --keep

The combined result lands in ``evals/results/sweep-<ts>.json``; each model's
own report is written alongside as ``run-sweep-<ts>-<model>.json``.
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
) -> dict | None:
    """Boot an agent on ``model``, run the suite, return the report dict."""
    base_url = f"http://127.0.0.1:{port}"
    report_path = _RESULTS_DIR / f"run-sweep-{ts}-{_slug(model)}.json"
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
        [sys.executable, "server.py", "--port", str(port), "--ui", "none"],
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
        subprocess.run(cmd, cwd=str(_PROJECT_ROOT), env=env, check=False)

        if not report_path.exists():
            print(f"  ✗ no report written for {model}")
            return None
        return json.loads(report_path.read_text())
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the eval suite across multiple models.")
    p.add_argument("--models", required=True, help="comma-separated model aliases")
    p.add_argument("--category", default=None, help="restrict to one eval category")
    p.add_argument("--tasks", default=None, help="comma-separated case IDs")
    p.add_argument("--port-base", type=int, default=7990, help="first port (each model uses port-base+i)")
    p.add_argument("--keep", action="store_true", help="keep each model's instance data + logs")
    args = p.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        sys.stderr.write("no models given\n")
        return 2

    ts = int(time.time())
    reports: dict[str, dict] = {}
    for i, model in enumerate(models):
        rep = _run_one_model(
            model,
            port=args.port_base + i,
            instance=f"eval-sweep-{ts}-{i}",
            ts=ts,
            category=args.category,
            tasks=args.tasks,
            keep=args.keep,
        )
        if rep is not None:
            reports[model] = rep

    if not reports:
        sys.stderr.write("no model produced a report\n")
        return 1

    matrix = _render_matrix(reports)
    print("\n" + matrix)

    combined = _RESULTS_DIR / f"sweep-{ts}.json"
    combined.write_text(json.dumps({
        "ts": ts,
        "models": models,
        "reports": {m: rep for m, rep in reports.items()},
    }, indent=2))
    (_RESULTS_DIR / f"sweep-{ts}.md").write_text(matrix + "\n")
    print(f"\nSweep: {combined}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
