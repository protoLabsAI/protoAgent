"""PTC graduation bench (ADR 0103, #2807) — the eval-suite harness, one command.

Generates deterministic fixtures + a two-lane eval tasks file, runs it through
``evals.runner`` against a live agent, then reads the per-turn telemetry back
and judges the PRE-REGISTERED graduation thresholds. The lanes:

- **loop** — the model must read each file with the ordinary ``read_file`` tool
  (``forbidden_tools: ["execute_code"]`` asserts compliance off the audit log).
- **code** — the model must batch the reads through ONE ``execute_code`` script
  (``expected_tools: ["execute_code", "ptc:read_file"]`` asserts the bridge
  actually fired — the ``ptc:`` audit prefix from ADR 0103 S3 is what makes the
  lane provable; ``forbidden_tools: ["read_file"]`` rejects a run that quietly
  fell back to the loop).

Correctness is symmetric and fair: each fixture file carries its answer as a
labeled first line (``SIZE: <n>``) so neither lane has to count characters —
the eval verifier asserts every file's name AND labeled value appear in the
reply. A cheaper-but-wrong lane fails, which is the point of running this as an
eval rather than a stopwatch script.

Pre-registered graduation thresholds (posted on #2807 BEFORE any run; the
numbers judge, not vibes) — over ``--reps`` repetitions per lane:

- rounds:  code-lane llm_calls ≤ 1/RQ_ROUNDS of loop-lane (default ≥5x collapse)
- economy: code-lane input tokens ≤ 1/RQ_TOKENS of loop-lane (≥3x) OR
           code-lane wall-clock ≤ 1/RQ_WALL of loop-lane (≥2x)
- correctness: code-lane eval pass-rate ≥ loop-lane's, and 100% on the reps

Usage (agent ≥ v0.138.0, execute_code plugin enabled, model configured)::

    # 1. fixtures + the registration snippet (once)
    python -m evals.ptc_bench fixtures --dir /path/to/ptc-bench-files

    # 2. the run: generate tasks, drive evals.runner, judge
    python -m evals.ptc_bench run --project <registered-name> \
        --base-url http://127.0.0.1:7871 --reps 2

The comparison MUST run with prompt caching on (the default): the #2777
history breakpoints made loop rounds cache-cheap, and benching against an
uncached world would flatter PTC dishonestly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# ── pre-registered thresholds (mirrored on #2807 — change there first) ────────
RQ_ROUNDS = 5.0  # loop rounds / code rounds must be ≥ this
RQ_TOKENS = 3.0  # loop input tokens / code input tokens ≥ this, OR…
RQ_WALL = 2.0  # …loop wall-clock / code wall-clock ≥ this

# Distinctive labeled sizes — 4-digit values that won't collide with incidental
# numbers in a reply. len == the file count ceiling.
_SIZES = [3107, 3613, 4127, 4639, 5153, 5669, 6173, 6689, 7207, 7717]


def _fixture_text(i: int) -> str:
    """Deterministic body: the labeled answer first, padding after."""
    size = _SIZES[i]
    head = f"SIZE: {size}\nFILE: f{i}.txt\n"
    filler = f"line of deterministic fixture text for f{i} · " * 200
    return head + filler[: max(0, size - len(head))]


def write_fixtures(directory: Path, files: int) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(min(files, len(_SIZES))):
        (directory / f"f{i}.txt").write_text(_fixture_text(i), encoding="utf-8")
        names.append(f"f{i}.txt")
    return names


def build_tasks(project: str, files: int, reps: int) -> list[dict]:
    names = [f"f{i}.txt" for i in range(min(files, len(_SIZES)))]
    listing = ", ".join(names)
    patterns = [str(_SIZES[i]) for i in range(len(names))]
    cases = []
    for rep in range(reps):
        cases.append(
            {
                "id": f"ptc_loop_{rep}",
                "category": "ptc-loop",
                "kind": "ask",
                "name": f"loop lane rep {rep}: one read_file per file",
                "context_id": f"ptc-eval-loop-{rep}",
                "prompt": (
                    f"Read each of these {len(names)} files one at a time with the read_file tool — "
                    f"do NOT use execute_code. Every file starts with a 'SIZE:' line; reply with one "
                    f"line per file: its name and that SIZE value. Files, all in the project "
                    f"'{project}': {listing}."
                ),
                "timeout_s": 600,
                "expected_tools": ["read_file"],
                "forbidden_tools": ["execute_code"],
                "expected_patterns": patterns,
            }
        )
        cases.append(
            {
                "id": f"ptc_code_{rep}",
                "category": "ptc-code",
                "kind": "ask",
                "name": f"code lane rep {rep}: one execute_code batches the reads",
                "context_id": f"ptc-eval-code-{rep}",
                "prompt": (
                    f"Use ONE execute_code call to read all of these {len(names)} files (the script's "
                    f"`tools` object has read_file) and print one line per file: its name and the value "
                    f"on its 'SIZE:' first line. Then reply with the script's output — do not call "
                    f"read_file directly yourself. Files, all in the project '{project}': {listing}."
                ),
                "timeout_s": 600,
                # The bridge must PROVABLY fire (ptc: audit prefix, ADR 0103 S3);
                # a quiet fallback to the direct tool fails the lane.
                "expected_tools": ["execute_code", "ptc:read_file"],
                "forbidden_tools": ["read_file"],
                "expected_patterns": patterns,
            }
        )
    return cases


# ── telemetry join + verdict ──────────────────────────────────────────────────


def _lane_rows(turns: list[dict], lane: str, reps: int) -> list[dict]:
    wanted = {f"ptc-eval-{lane}-{rep}" for rep in range(reps)}
    return [t for t in turns if any(str(t.get("session_id", "")).endswith(w) for w in wanted)]


def _agg(rows: list[dict]) -> dict:
    n = max(1, len(rows))
    return {
        "turns": len(rows),
        "rounds": sum(int(r.get("llm_calls", 0) or 0) for r in rows) / n,
        "input_tokens": sum(int(r.get("input_tokens", 0) or 0) for r in rows) / n,
        "cache_read": sum(int(r.get("cache_read_input_tokens", 0) or 0) for r in rows) / n,
        "cost_usd": sum(float(r.get("cost_usd", 0) or 0) for r in rows) / n,
        "wall_ms": sum(int(r.get("duration_ms", 0) or 0) for r in rows) / n,
    }


def _pass_rate(report: dict, category: str) -> tuple[int, int]:
    results = report.get("results") or report.get("cases") or []
    rows = [r for r in results if r.get("category") == category]
    return sum(1 for r in rows if r.get("passed") or r.get("ok")), len(rows)


def verdict(loop: dict, code: dict, loop_pass: tuple[int, int], code_pass: tuple[int, int]) -> dict:
    """The pre-registered judgment. Ratios use loop/code so bigger = better for PTC."""
    rounds_x = loop["rounds"] / max(1e-9, code["rounds"])
    tokens_x = loop["input_tokens"] / max(1e-9, code["input_tokens"])
    wall_x = loop["wall_ms"] / max(1e-9, code["wall_ms"])
    correctness_ok = code_pass[1] > 0 and code_pass[0] == code_pass[1] and (
        (code_pass[0] / code_pass[1]) >= ((loop_pass[0] / loop_pass[1]) if loop_pass[1] else 0)
    )
    graduated = rounds_x >= RQ_ROUNDS and (tokens_x >= RQ_TOKENS or wall_x >= RQ_WALL) and correctness_ok
    return {
        "rounds_x": round(rounds_x, 2),
        "tokens_x": round(tokens_x, 2),
        "wall_x": round(wall_x, 2),
        "correctness_ok": correctness_ok,
        "graduated": graduated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fixtures", help="write the deterministic fixture files")
    f.add_argument("--dir", required=True)
    f.add_argument("--files", type=int, default=10)

    r = sub.add_parser("run", help="generate tasks, run evals.runner, judge the thresholds")
    r.add_argument("--project", required=True, help="registered filesystem project holding the fixtures")
    r.add_argument("--base-url", default="http://127.0.0.1:7871")
    r.add_argument("--files", type=int, default=10)
    r.add_argument("--reps", type=int, default=2)
    args = ap.parse_args()

    if args.cmd == "fixtures":
        names = write_fixtures(Path(args.dir), args.files)
        print(f"wrote {len(names)} fixture file(s) to {args.dir}")
        print("register the dir as a project (Settings ▸ Capabilities, or config):")
        print(f"  projects:\n    - name: ptcbench\n      path: {args.dir}\n      write: false")
        return 0

    tasks = build_tasks(args.project, args.files, args.reps)
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    stamp = int(time.time())
    tasks_path = results_dir / f"ptc-tasks-{stamp}.json"
    tasks_path.write_text(json.dumps(tasks, indent=1), encoding="utf-8")
    print(f"tasks → {tasks_path} ({len(tasks)} cases, {args.reps} rep(s)/lane)")

    before = set(results_dir.glob("run-*.json"))
    rc = subprocess.call(
        [sys.executable, "-m", "evals.runner", "--tasks-file", str(tasks_path), "--base-url", args.base_url]
    )
    new_reports = sorted(set(results_dir.glob("run-*.json")) - before)
    if not new_reports:
        print("no runner report found — runner failed before writing one", file=sys.stderr)
        return rc or 1
    report = json.loads(new_reports[-1].read_text())

    import httpx

    import os

    headers = {}
    if os.environ.get("A2A_AUTH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['A2A_AUTH_TOKEN']}"
    body = httpx.get(
        f"{args.base_url}/api/telemetry/recent", params={"limit": 200}, timeout=30, headers=headers
    ).json()
    # The route returns {"enabled":…, "turns":[…], …} — take the list, never fall
    # back to iterating the dict (its KEYS are strings; the first live run
    # crashed exactly there).
    turns = body.get("turns") or [] if isinstance(body, dict) else (body or [])
    loop_rows, code_rows = _lane_rows(turns, "loop", args.reps), _lane_rows(turns, "code", args.reps)
    loop, code = _agg(loop_rows), _agg(code_rows)
    loop_pass, code_pass = _pass_rate(report, "ptc-loop"), _pass_rate(report, "ptc-code")
    v = verdict(loop, code, loop_pass, code_pass)

    print(f"\n{'lane':<6} {'pass':>7} {'rounds':>7} {'in-tok':>10} {'cache-rd':>10} {'cost':>9} {'wall':>8}")
    for label, agg, p in (("loop", loop, loop_pass), ("code", code, code_pass)):
        print(
            f"{label:<6} {p[0]}/{p[1]:<5} {agg['rounds']:>7.1f} {agg['input_tokens']:>10.0f} "
            f"{agg['cache_read']:>10.0f} ${agg['cost_usd']:>8.4f} {agg['wall_ms'] / 1000:>7.1f}s"
        )
    print(
        f"\ncollapse: rounds {v['rounds_x']}x (need ≥{RQ_ROUNDS}) · tokens {v['tokens_x']}x "
        f"(need ≥{RQ_TOKENS}) OR wall {v['wall_x']}x (need ≥{RQ_WALL}) · "
        f"correctness {'OK' if v['correctness_ok'] else 'FAILED'}"
    )
    print(f"\nVERDICT: {'GRADUATED — proceed to ADR 0103 S4/GA' if v['graduated'] else 'NOT GRADUATED — post the numbers on #2807 and reassess'}")
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    raise SystemExit(main())
