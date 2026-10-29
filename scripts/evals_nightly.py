#!/usr/bin/env python
"""Nightly eval gate — sweep the default model, gate GATE-tier regressions, flag flakes.

Runs the eval suite against the **default config's model** on a *throwaway*
instance, writes a marked nightly report to ``evals/results/``, compares the
**gate tier** (ADR 0126 D1) with the previous nightly, and turns each gate
regression into one harness friction entry. Flaky gate cases are printed for a
human to deal with — nothing auto-demotes.

What runs where (ADR 0004 instance scoping — read this, it is the whole
isolation story):

  * **The model under test runs on a throwaway instance.** The sweep
    (``evals.sweep``) boots a UI-less agent on its own ``PROTOAGENT_INSTANCE``
    (``eval-sweep-<ts>-0``) and its own port, runs the suite, and tears the
    instance down. A live agent's chat/tasks/knowledge are never touched — the
    throwaway name is passed only into the *sweep subprocess's* environment, not
    this process's.

  * **Friction is recorded into THIS process's instance ledger.** The regression
    → friction write goes through the friction plugin's ``record_service``, which
    resolves the ledger at ``instance_paths().store("friction")`` under the
    environment of *this* process. So a regression is recorded against whatever
    instance you run the nightly under — the host/default instance in production
    (``PROTOAGENT_INSTANCE`` unset → ``default``), or the dev sandbox when you run
    it under ``scripts/dev.sh``'s ``PROTOAGENT_INSTANCE=dev``. It is deliberately
    NOT the throwaway: the throwaway is deleted when the sweep ends, so friction
    written there would vanish with it. The nightly runs outside any agent turn,
    so it reaches the ledger by importing the plugin's service directly (the
    ``graph.sdk.record_friction`` seam no-ops here — it resolves a *registered*
    plugin service, and nothing is registered in a bare script process).

Marking: the nightly report is written as ``run-nightly-<ts>.json`` carrying
``"nightly": true``. It matches ``run-*.json`` so ``python -m evals.report``
trends it like any other run, but the previous-nightly comparison only ever
reads ``run-nightly-*.json`` — an ad-hoc ``python -m evals.sweep`` /
``python -m evals.runner`` is never mistaken for last night.

Usage::

    python scripts/evals_nightly.py                 # default model, default instance
    PROTOAGENT_INSTANCE=dev python scripts/evals_nightly.py   # the :7871 sandbox ledger
    python scripts/evals_nightly.py --model vendor/some-model
    python scripts/evals_nightly.py --dry-run       # analyse the last two nightlies, record nothing

Installing the schedule (a launchd plist on macOS, or cron on Linux) is an
operator step on the host — see ``docs/guides/evals.md``. This script installs
nothing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from evals.compare import resolve_tier  # noqa: E402

_RESULTS_DIR = _PROJECT_ROOT / "evals" / "results"

# The friction a gate regression records. ``source`` is stamped on the ledger row
# as ``plugin:evals-nightly``; ``kind`` is ``harness`` because a gate break is a
# property of the agent/harness under test, not a model-turn mistake.
FRICTION_SOURCE = "evals-nightly"
FRICTION_KIND = "harness"
# A gate break is the deterministic "this still works" contract failing — major.
FRICTION_SEVERITY = "major"

# Flake rule (ADR 0126 D1): a gate case that flips pass/fail this many times inside
# the last N nightly reports is flaky — printed, never auto-demoted.
FLAKE_WINDOW = 7
FLAKE_MIN_FLIPS = 2
FLAKE_ADVICE = "demote to tracked or fix"


# ── pure analysis (unit-tested with fakes) ───────────────────────────────────


def gate_rows(report: dict) -> dict[str, dict]:
    """``case_id -> row`` for the report's non-skipped, GATE-tier rows.

    Each row's tier is resolved with :func:`evals.compare.resolve_tier`, so a
    legacy report whose rows predate the ``tier`` field still splits (those
    default to ``gate``). Skipped cases carry no pass/fail signal and are dropped
    — a case gated behind an unset ``requires_env`` must not read as a regression
    the night its prerequisite goes missing."""
    out: dict[str, dict] = {}
    for r in report.get("results", []):
        if r.get("skipped"):
            continue
        if resolve_tier(r) != "gate":
            continue
        out[str(r.get("id"))] = r
    return out


def gate_regressions(prev: dict, curr: dict) -> list[dict]:
    """Gate cases that **passed in ``prev`` and fail in ``curr``** — the current rows.

    Only cases that are gate in BOTH reports count: a tracked-tier flip never
    gates (its rubric score is a trend, not a contract — ADR 0126 D1), and a case
    newly introduced this night has no prior pass to regress from. Sorted by id so
    the recorded friction order is stable."""
    prev_gate = gate_rows(prev)
    curr_gate = gate_rows(curr)
    regressed = [
        row
        for cid, row in curr_gate.items()
        if not row.get("passed") and prev_gate.get(cid, {}).get("passed")
    ]
    return sorted(regressed, key=lambda r: str(r.get("id")))


def _gate_sequences(reports: list[dict]) -> dict[str, list[bool]]:
    """``case_id -> [passed, passed, …]`` across ``reports`` in order, gate rows only.

    A case missing from a report (not run, or skipped) simply contributes no
    entry for that report, so its flip count is measured over the nights it
    actually ran rather than treating an absence as a fail."""
    seqs: dict[str, list[bool]] = defaultdict(list)
    for rep in reports:
        for cid, row in gate_rows(rep).items():
            seqs[cid].append(bool(row.get("passed")))
    return seqs


def flaky_gate_cases(
    reports: list[dict], *, window: int = FLAKE_WINDOW, min_flips: int = FLAKE_MIN_FLIPS
) -> list[tuple[str, int]]:
    """Gate cases that flip pass/fail ``>= min_flips`` times within the last
    ``window`` nightly reports → ``[(case_id, flips), …]`` sorted by id.

    A "flip" is a pass↔fail transition between consecutive runs of the same case,
    so ``pass, fail, pass`` is two flips. This finds the cases that are *unstable*
    on the gate — a one-off regression that stays failing has one flip and is a
    regression, not a flake; a case that comes and goes is the one the operator
    should demote to tracked or fix."""
    recent = reports[-window:] if window else reports
    flaky: list[tuple[str, int]] = []
    for cid, seq in _gate_sequences(recent).items():
        flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        if flips >= min_flips:
            flaky.append((cid, flips))
    return sorted(flaky, key=lambda t: t[0])


def _default_trace_link(trace_id: str) -> str:
    """A Langfuse trace URL for ``trace_id`` when tracing is live, else the raw id.

    Reuses the console's own resolver (``<host>/project/<id>/traces/{trace_id}``)
    so the link shape matches the one the friction view renders; when Langfuse is
    off there is no template and the raw id is still worth recording so a human can
    find the run."""
    trace_id = (trace_id or "").strip()
    if not trace_id:
        return ""
    try:
        from urllib.parse import quote

        from operator_api.telemetry_routes import _resolve_trace_url_template

        template = _resolve_trace_url_template()
    except Exception:  # noqa: BLE001 — a tracing blip must not stop friction recording
        template = None
    if template:
        return template.replace("{trace_id}", quote(trace_id, safe=""))
    return trace_id


def _friction_recorder():
    """The friction ``record_service`` callable, or ``None`` when it can't be reached.

    Imported directly off the plugin module (not through ``graph.sdk``) because the
    nightly runs outside an agent turn, with no registered plugin services. Returns
    ``None`` if the friction plugin isn't importable — recording degrades to a
    printed warning rather than crashing the gate."""
    try:
        from plugins.friction import record_service

        return record_service
    except Exception:  # noqa: BLE001
        return None


def record_regressions(
    regressions: list[dict],
    report_path: str | Path,
    *,
    recorder=None,
    trace_link=None,
) -> list[str]:
    """Record one ``harness`` friction entry per regressed gate case → the ids written.

    The summary is STABLE — ``nightly gate regression: <case id>`` with no date or
    count — because the ledger groups by ``(kind, summary)`` (friction plugin): a
    case that regresses three nights running is ONE row seen three times, not three
    rows. The per-night specifics (the report path, the trace link from the row's
    ``trace_id``) go in the detail, which varies freely. ``recorder`` /
    ``trace_link`` are injectable for tests; the defaults reach the live plugin and
    the live tracer."""
    recorder = recorder or _friction_recorder()
    trace_link = trace_link or _default_trace_link
    if recorder is None:
        print("  ! friction plugin unavailable — gate regressions NOT recorded", file=sys.stderr)
        return []
    written: list[str] = []
    for row in regressions:
        cid = str(row.get("id"))
        trace_id = str(row.get("trace_id", "") or "")
        link = trace_link(trace_id)
        detail = f"report: {report_path}"
        if link:
            detail += f"\ntrace: {link}"
        recorder(
            FRICTION_KIND,
            f"nightly gate regression: {cid}",
            detail,
            FRICTION_SEVERITY,
            source=FRICTION_SOURCE,
            trace_id=trace_id,
        )
        written.append(cid)
    return written


def format_flakes(flakes: list[tuple[str, int]], *, window: int = FLAKE_WINDOW) -> str:
    """Render the flaky-gate block, each line carrying the ``demote to tracked or
    fix`` advice. Empty string when nothing is flaky."""
    if not flakes:
        return ""
    lines = [f"Flaky gate cases (flipped pass/fail within the last {window} nightlies):"]
    for cid, flips in flakes:
        lines.append(f"  FLAKY {cid}: flipped {flips}× — {FLAKE_ADVICE}")
    return "\n".join(lines)


# ── report IO ────────────────────────────────────────────────────────────────


def _read_report(path: Path) -> dict | None:
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return rep if isinstance(rep, dict) and "results" in rep else None


def load_nightly_reports(results_dir: Path) -> list[dict]:
    """Every marked nightly report (``run-nightly-*.json`` with ``nightly: true``),
    oldest first by ``nightly_ts`` (then ``ts``).

    Only these feed the previous-nightly comparison and the flake window, so an
    ad-hoc sweep/runner report sharing the directory is never read as last
    night."""
    out: list[dict] = []
    for path in sorted(results_dir.glob("run-nightly-*.json")):
        rep = _read_report(path)
        if rep is None or not rep.get("nightly"):
            continue
        rep["_file"] = path.name
        out.append(rep)
    out.sort(key=lambda r: (r.get("nightly_ts", 0), r.get("ts", "")))
    return out


def mark_nightly(sweep_report: Path, ts: int, results_dir: Path) -> Path:
    """Stamp a sweep's per-model report as the night's nightly and rename it to
    ``run-nightly-<ts>.json``.

    Renamed rather than copied so there is exactly one report in ``results/`` for
    the run (no double-count in ``evals.report``); the ``run-nightly-`` prefix and
    the ``nightly: true`` flag are what mark it."""
    rep = _read_report(sweep_report) or {}
    rep["nightly"] = True
    rep["nightly_ts"] = ts
    dest = results_dir / f"run-nightly-{ts}.json"
    dest.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    try:
        sweep_report.unlink()
    except OSError:
        pass
    return dest


# ── sweep orchestration (side-effectful; not unit-tested) ─────────────────────


def default_model() -> str:
    """The default config's lead model (``model.name``), honoring ``PROTOAGENT_MODEL``.

    Resolved under this process's instance, so ``PROTOAGENT_INSTANCE=dev`` reads the
    dev sandbox's config — the same config the sweep would boot the throwaway from."""
    from graph.config import LangGraphConfig
    from graph.config_io import config_example_path, config_yaml_path

    path = config_yaml_path()
    if not path.is_file():
        path = Path(config_example_path())
    try:
        return LangGraphConfig.from_yaml(path).model_name
    except Exception:  # noqa: BLE001
        return LangGraphConfig.app_defaults().model_name


def run_sweep(model: str, *, port_base: int, results_dir: Path) -> Path | None:
    """Run ``python -m evals.sweep --models <model>`` (one throwaway instance) and
    return the per-model report it wrote, or ``None`` if the sweep produced none.

    The new ``run-sweep-*.json`` is found by diffing the results dir around the
    run, so the sweep's self-chosen timestamp doesn't have to be guessed."""
    before = set(results_dir.glob("run-sweep-*.json"))
    cmd = [
        sys.executable,
        "-m",
        "evals.sweep",
        "--models",
        model,
        "--port-base",
        str(port_base),
    ]
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(_PROJECT_ROOT), check=False)
    new = sorted(set(results_dir.glob("run-sweep-*.json")) - before, key=lambda p: p.stat().st_mtime)
    return new[-1] if new else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=None, help="model to sweep (default: the default config's model.name)")
    p.add_argument("--port-base", type=int, default=7990, help="port for the throwaway sweep agent")
    p.add_argument("--results-dir", default=str(_RESULTS_DIR), help="where reports live")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="skip the sweep; just compare the two most recent nightlies + flag flakes (records nothing)",
    )
    args = p.parse_args(argv)

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        nightlies = load_nightly_reports(results_dir)
        if len(nightlies) < 2:
            print("dry-run: need at least two nightly reports to compare", file=sys.stderr)
            return 1
        prev, curr = nightlies[-2], nightlies[-1]
        regressions = gate_regressions(prev, curr)
        print(f"dry-run: {len(regressions)} gate regression(s) vs {prev['_file']} (not recorded)")
        for row in regressions:
            print(f"  REGRESSED {row.get('id')}")
        flakes = flaky_gate_cases(nightlies)
        if flakes:
            print(format_flakes(flakes))
        return 0

    model = args.model or default_model()
    ts = int(time.time())
    print(f"nightly eval sweep — model={model} (throwaway instance; friction → this instance's ledger)")

    sweep_report = run_sweep(model, port_base=args.port_base, results_dir=results_dir)
    if sweep_report is None:
        print("sweep produced no report (agent never became healthy?) — nothing to gate", file=sys.stderr)
        return 1

    report_path = mark_nightly(sweep_report, ts, results_dir)
    print(f"nightly report: {report_path}")

    nightlies = load_nightly_reports(results_dir)
    curr = nightlies[-1]
    # nightlies is oldest-first and ends with the run just written, so the previous
    # nightly is the one before it (if any).
    prev = nightlies[-2] if len(nightlies) >= 2 else None

    recorded: list[str] = []
    if prev is None:
        print("no previous nightly — recording a baseline, gating nothing this run")
    else:
        regressions = gate_regressions(prev, curr)
        recorded = record_regressions(regressions, report_path)
        if recorded:
            print(f"gate regressions vs {prev['_file']}: {len(recorded)} recorded as friction ({FRICTION_SOURCE}):")
            for cid in recorded:
                print(f"  REGRESSED {cid} → friction 'nightly gate regression: {cid}'")
        else:
            print(f"no gate regressions vs {prev['_file']}")

    flakes = flaky_gate_cases(nightlies)
    if flakes:
        print(format_flakes(flakes))

    print("\nsee the gate/tracked split with:  python -m evals.report")
    # Exit non-zero when the gate regressed, so a host scheduler can surface a failed run.
    return 1 if recorded else 0


if __name__ == "__main__":
    raise SystemExit(main())
