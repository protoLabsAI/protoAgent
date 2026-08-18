"""PTC spike bench (ADR 0103 S1, #2807) — measure loop-mode vs code-mode on a LIVE agent.

The deterministic half of the spike lives in ``tests/test_execute_code.py``
(``test_ptc_collapse_mechanics_ten_reads_one_round``): ten bridged reads behind
one model-visible result, output <0.1% of the intermediate bytes. This script is
the model-in-the-loop half the ADR gates on: it drives a real instance through
the same investigation twice and reads the per-turn telemetry back.

Usage (against the dev sandbox by default — never point it at a production agent
mid-work):

    uv run python scripts/ptc_bench.py --base http://127.0.0.1:7871 \
        --project <a registered fs project> --files 10

Requires: the target instance running with the execute_code plugin enabled and
the named project registered/fenced. Prints one table: rounds (llm_calls),
input/output tokens, cache reads, cost, duration for each mode.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

LOOP_PROMPT = (
    "Read each of these {n} files one at a time with the read_file tool — do NOT use "
    "execute_code — and then reply with one line per file: its name and its size in "
    "characters. Files, all in the project '{project}': {files}."
)

PTC_PROMPT = (
    "Use ONE execute_code call to read all of these {n} files (the script's `tools` "
    "object has read_file) and print one line per file: its name and its size in "
    "characters. Then reply with the script's output. Files, all in the project "
    "'{project}': {files}."
)


def _turn(client: httpx.Client, base: str, prompt: str, session_id: str) -> None:
    r = client.post(f"{base}/api/chat", json={"message": prompt, "session_id": session_id}, timeout=600)
    r.raise_for_status()


def _latest_turn(client: httpx.Client, base: str, session_id: str) -> dict | None:
    r = client.get(f"{base}/api/telemetry/recent", params={"limit": 50}, timeout=30)
    r.raise_for_status()
    turns = r.json().get("turns") or r.json() if isinstance(r.json(), dict) else r.json()
    if isinstance(turns, dict):
        turns = turns.get("turns", [])
    for t in turns:
        if t.get("session_id", "").endswith(session_id):
            return t
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:7871", help="live instance base URL (dev sandbox default)")
    ap.add_argument("--project", required=True, help="a registered filesystem project name")
    ap.add_argument("--files", type=int, default=10, help="how many files to read (they must exist)")
    ap.add_argument("--file-pattern", default="README.md", help="comma-list or single path repeated")
    args = ap.parse_args()

    names = [p.strip() for p in args.file_pattern.split(",") if p.strip()]
    files = (names * args.files)[: args.files]
    stamp = int(time.time())

    with httpx.Client() as client:
        rows = []
        for label, prompt in (("loop", LOOP_PROMPT), ("ptc", PTC_PROMPT)):
            sid = f"ptc-bench-{label}-{stamp}"
            print(f"[{label}] running…", flush=True)
            _turn(
                client,
                args.base,
                prompt.format(n=len(files), project=args.project, files=", ".join(files)),
                sid,
            )
            t = _latest_turn(client, args.base, sid)
            if t is None:
                print(f"[{label}] no telemetry row found for {sid}", file=sys.stderr)
                continue
            rows.append((label, t))

    if len(rows) == 2:
        print(f"\n{'mode':<6} {'rounds':>6} {'in-tok':>10} {'cache-rd':>10} {'out-tok':>8} {'cost':>10} {'ms':>8}")
        for label, t in rows:
            print(
                f"{label:<6} {t.get('llm_calls', 0):>6} {t.get('input_tokens', 0):>10} "
                f"{t.get('cache_read_input_tokens', 0):>10} {t.get('output_tokens', 0):>8} "
                f"${t.get('cost_usd', 0):>9.4f} {t.get('duration_ms', 0):>8}"
            )
        loop_t, ptc_t = rows[0][1], rows[1][1]
        if loop_t.get("llm_calls") and ptc_t.get("llm_calls"):
            print(
                f"\ncollapse: {loop_t['llm_calls']}→{ptc_t['llm_calls']} rounds · "
                f"input tokens {loop_t['input_tokens']}→{ptc_t['input_tokens']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
