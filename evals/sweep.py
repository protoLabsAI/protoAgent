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

``--prior-sessions newest,off`` adds a CONFIG axis: each value becomes its own arm
(cross-producted with ``--models``), booted from a seed config carrying that
``context.prior_sessions`` policy. That is the vehicle for the #3186 evaluation —
"is the always-on prior-session digest worth its turn cost, now that session_search
exists?" — and it is why arms seed their session fixtures BEFORE boot: the digest's
entry pool is cached for 60 s per process, so a summary written after boot may not
be in the first turns' digest at all.

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

from evals import verify  # noqa: E402
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


def _instance_memory_dir(instance: str) -> Path:
    """Where that instance's session summaries live — the digest's source.

    Resolved through ``infra.paths`` under the ARM's environment rather than
    joined by hand (PROTO.md: never compute store paths yourself). The hand-rolled
    ``~/.protoagent/<instance>/memory`` is right on a plain host install and wrong
    everywhere else — ``PROTOAGENT_HOME`` (the desktop) makes the instance root
    that directory itself, and ``PROTOAGENT_BOX_ROOT`` moves the whole tree. The
    failure would have been silent: fixtures land where the arm never looks,
    seeding reports success, and the A/B measures an empty digest.
    """
    from infra.paths import instance_paths, reset_instance_paths

    previous = os.environ.get("PROTOAGENT_INSTANCE")
    os.environ["PROTOAGENT_INSTANCE"] = instance
    reset_instance_paths()  # the singleton caches the FIRST resolution
    try:
        return instance_paths().store("memory")
    finally:
        if previous is None:
            os.environ.pop("PROTOAGENT_INSTANCE", None)
        else:
            os.environ["PROTOAGENT_INSTANCE"] = previous
        reset_instance_paths()  # never leave this process pointed at the arm


def _yaml_quoted(value: str) -> object:
    """A scalar that survives the dump as a STRING.

    ``off`` is the policy whose name YAML 1.1 also spells as the boolean False
    (#3254), and ruamel dumps a bare Python ``str`` bare. The config layer happens
    to restore ``False`` to ``"off"``, but a seed config that says ``off`` and means
    ``False`` is a trap waiting for the next reader — so quote it at the source.
    PyYAML already quotes it; this only has to teach ruamel.
    """
    try:
        from ruamel.yaml.scalarstring import DoubleQuotedScalarString

        return DoubleQuotedScalarString(str(value))
    except Exception:  # noqa: BLE001 — no ruamel → PyYAML quotes it itself
        return str(value)


def _seed_config_for(policy: str, ts: int, slug: str) -> Path | None:
    """A config file for one arm, carrying ``context.prior_sessions: <policy>``.

    Handed to the arm through ``PROTOAGENT_SEED_CONFIG`` — the documented
    seed-a-fresh-instance seam — so the throwaway instance comes up already
    configured instead of being patched after boot (the policy is read at boot).

    The base is the operator's own live config when there is one, so an arm talks
    to the same gateway the real agent does; otherwise the bundled ``.example``.
    Secrets are NOT copied: ``api_key`` falls back to ``OPENAI_API_KEY``, which is
    how the sweep has always fed its throwaway agents.
    """
    from graph.config_io import config_example_path, config_yaml_path, load_yaml_doc, save_yaml_doc

    try:
        base = config_yaml_path()
        if not base.is_file():
            base = Path(config_example_path())
        doc = load_yaml_doc(base)
        if not isinstance(doc, dict):
            return None
        doc.setdefault("context", {})
        if not isinstance(doc["context"], dict):
            doc["context"] = {}
        doc["context"]["prior_sessions"] = _yaml_quoted(policy)
        out = _RESULTS_DIR / f"seed-config-{ts}-{slug}.yaml"
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        save_yaml_doc(doc, out)
        return out
    except Exception as exc:  # noqa: BLE001 — an unseedable arm is reported, not fatal
        print(f"  ! could not build a seed config for prior_sessions={policy}: {exc}")
        return None


def _session_seed_steps(category: str | None, tasks: str | None) -> list[dict]:
    """Every ``session_seed`` setup step of the cases this run will execute.

    Seeded before the arm boots — see ``_run_one_model``; an arm running with
    ``prior_sessions: off`` needs the same summaries on disk either way, since
    that is the whole comparison.
    """
    from evals.runner import select_by_ids

    cases = json.loads((Path(__file__).parent / "tasks.json").read_text())
    # AND, exactly like the runner composes them (--tasks X --category Y runs the
    # intersection). Filtering differently here would seed one set and run another.
    if category:
        cases = [c for c in cases if c.get("category") == category]
    if tasks:
        cases, unknown = select_by_ids(cases, tasks)
        for cid in unknown:
            print(f"  ! --tasks names {cid!r}, which no selected case matches")
    steps: list[dict] = []
    for c in cases:
        for step in c.get("setup") or []:
            if "session_seed" in step:
                steps.append(step)
    return steps


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
    label: str | None = None,
    prior_sessions: str | None = None,
    seed_steps: list[dict] | None = None,
) -> list[dict] | None:
    """Boot one agent on ``model`` and run the suite ``repeat`` times against
    it; return the list of report dicts (empty if the agent never came up).

    Repeats run against the *same* booted agent on purpose — that isolates the
    model's own run-to-run sampling variance (the thing best-of-N measures) from
    boot/cold-start variance, and costs one boot per model instead of N."""
    base_url = f"http://127.0.0.1:{port}"
    log_path = _RESULTS_DIR / f"server-sweep-{ts}-{_slug(label or model)}.log"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    label = label or model
    env = {
        **os.environ,
        "PROTOAGENT_MODEL": model,
        "PROTOAGENT_INSTANCE": instance,
        "PROTOAGENT_UI": "none",
    }
    if prior_sessions:
        seed_cfg = _seed_config_for(prior_sessions, ts, _slug(label))
        if seed_cfg is None:
            print(f"  ✗ no seed config for prior_sessions={prior_sessions} — skipping this arm")
            return None
        env["PROTOAGENT_SEED_CONFIG"] = str(seed_cfg)
    # Fixtures land BEFORE boot: the digest pool is cached per process (60 s), so a
    # summary written after boot may be missing from the first turns' digest — which
    # would quietly measure the cache instead of the policy.  (The one statement of
    # this; the module docstring and docs/guides/evals.md point back here.)
    if seed_steps:
        mem = _instance_memory_dir(instance)
        err = verify.apply_setup(seed_steps, memory_dir=str(mem))
        if err:
            print(f"  ! session fixtures not seeded: {err}")
        else:
            print(f"  · seeded {len(seed_steps)} session fixture(s) into {mem}")
    # Give the throwaway agent a bearer token so the auth-gating eval cases are
    # actually exercised (an unconfigured instance accepts any token → the
    # negative-auth case can't pass). The runner's client reads the same env
    # var, so the good-token cases still authenticate. Respect a token the
    # operator already set.
    env.setdefault("A2A_AUTH_TOKEN", f"eval-sweep-{ts}")
    print(f"\n=== {label} :: booting on {base_url} (instance={instance}) ===")
    log_f = open(log_path, "w", encoding="utf-8")
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
            if seed_steps and run_i:
                # Each case tears down what it seeded, so run 1 leaves an empty
                # store behind. Re-seed between runs — by now the agent's 60 s
                # pool cache has long expired, so the next turn re-reads the dir.
                verify.apply_setup(seed_steps, memory_dir=str(_instance_memory_dir(instance)))
            suffix = f"-r{run_i + 1}" if repeat > 1 else ""
            report_path = _RESULTS_DIR / f"run-sweep-{ts}-{_slug(label)}{suffix}.json"
            cmd = [
                sys.executable,
                "-m",
                "evals.runner",
                "--base-url",
                base_url,
                "--model-label",
                label,
                "--out",
                str(report_path),
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
                print(f"  ✗ no report written for {label} (run {run_i + 1})")
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
        overall = (
            f"**{rep.get('passed', 0)}/{rep.get('total', 0)} ({_pct(rep.get('passed', 0), rep.get('total', 0))})**"
        )
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
    lines.append("| **Best-of-N passed** | " + " | ".join(f"**{best_of_n(m)}/{total}**" for m in ordered) + " |")
    lines.append("| Avg latency | " + " | ".join(f"{_avg_latency(model_runs[m])}ms" for m in ordered) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the eval suite across multiple models.")
    p.add_argument("--models", required=True, help="comma-separated model aliases")
    p.add_argument("--category", default=None, help="restrict to one eval category")
    p.add_argument("--tasks", default=None, help="comma-separated case IDs")
    p.add_argument("--port-base", type=int, default=7990, help="first port (each model uses port-base+i)")
    p.add_argument("--keep", action="store_true", help="keep each model's instance data + logs")
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run the suite N times per model for a best-of-N (majority) per-case table",
    )
    p.add_argument(
        "--prior-sessions",
        default="",
        help=(
            "comma-separated context.prior_sessions policies (newest|relevant|off) to run as "
            "separate arms — the #3186 A/B. Empty (default) = leave the config alone."
        ),
    )

    args = p.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        sys.stderr.write("no models given\n")
        return 2
    repeat = max(1, args.repeat)

    from graph.middleware.memory import PRIOR_SESSION_POLICIES

    policies = [p.strip() for p in (args.prior_sessions or "").split(",") if p.strip()]
    unknown = [p for p in policies if p not in PRIOR_SESSION_POLICIES]
    if unknown:
        sys.stderr.write(f"unknown prior_sessions policy: {', '.join(unknown)}\n")
        return 2
    # One arm per (model, policy). Without a policy axis an arm is just a model,
    # labelled exactly as before so existing reports keep their shape.
    arms = [(m, pol) for m in models for pol in (policies or [None])]
    seed_steps = _session_seed_steps(args.category, args.tasks)

    if os.environ.get("PROTOAGENT_HOME", "").strip() and (policies or seed_steps):
        # PROTOAGENT_HOME is terminal: the instance root IS that directory, and
        # PROTOAGENT_INSTANCE stops moving it. Arms would share one config and one
        # memory store — the seed config of the last arm booted, fixtures written
        # into the operator's OWN store, and a comparison of a policy against
        # itself. Refuse rather than produce a confident, meaningless matrix.
        sys.stderr.write(
            "PROTOAGENT_HOME is set, which pins every instance to one root — sweep arms cannot be "
            "isolated (they would share config and session memory). Unset it, or run the sweep on a "
            "plain host install.\n"
        )
        return 2

    ts = int(time.time())
    model_runs: dict[str, list[dict]] = {}
    for i, (model, policy) in enumerate(arms):
        # The label rides `--model-label` into the report, and evals.report trends
        # reports BY that label — so an arm always names its model, or a policy would
        # show up in the model leaderboard as if it were one.
        label = model if policy is None else f"{model} @ prior_sessions={policy}"
        runs = _run_one_model(
            model,
            port=args.port_base + i,
            instance=f"eval-sweep-{ts}-{i}",
            ts=ts,
            category=args.category,
            tasks=args.tasks,
            keep=args.keep,
            repeat=repeat,
            label=label,
            prior_sessions=policy,
            seed_steps=seed_steps,
        )
        if runs:
            model_runs[label] = runs

    if not model_runs:
        sys.stderr.write("no model produced a report\n")
        return 1

    if repeat > 1:
        matrix = _render_repeat_matrix(model_runs, repeat)
    else:
        matrix = _render_matrix({m: runs[0] for m, runs in model_runs.items()})
    print("\n" + matrix)

    combined = _RESULTS_DIR / f"sweep-{ts}.json"
    combined.write_text(
        json.dumps(
            {
                "ts": ts,
                "models": models,
                "repeat": repeat,
                # repeat>1: all N runs per model; repeat==1: the single run (list of one).
                "runs": model_runs,
            },
            indent=2,
        )
    )
    (_RESULTS_DIR / f"sweep-{ts}.md").write_text(matrix + "\n")
    print(f"\nSweep: {combined}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
