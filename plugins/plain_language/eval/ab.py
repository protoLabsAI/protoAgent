#!/usr/bin/env python
"""Plain-language A/B: does the plugin make the agent's writing clearer?

Boots two throwaway agents from the same seed config (the default instance's),
differing only in this plugin:

  * **Arm A (baseline)**: ``plugins.disabled: [plain_language]``
  * **Arm B (plain)**: ``plugins.enabled: [plain_language]``

Sends every writing task in ``prompts.json`` to both, ``--repeat`` times each, in
a fresh conversation every time. Then it scores three ways:

1. **Blind pairwise judge.** A grader model sees the reader, the request, and the
   two replies as "Response 1" and "Response 2". It never learns which arm wrote
   which, and it is never told about plain language. It is asked which reply that
   reader would understand faster and more accurately without losing anything they
   need. Each pair is judged twice with the order swapped, which cancels position
   bias. A pair counts as a win only when the two orderings don't disagree.
2. **Fact coverage.** Deterministic: the share of each task's required facts
   (``facts``) the reply kept. This catches "clearer" that means "dropped things".
3. **Lint.** ``lint.check`` weighted findings per 100 words, plus length.

Usage (from the repo root, with the repo's venv)::

    python plugins/plain_language/eval/ab.py
    python plugins/plain_language/eval/ab.py --repeat 3 --judge-model anthropic-oauth:claude-opus-5-5
    python plugins/plain_language/eval/ab.py --cases status_email,rewrite_notice --keep

Results land in ``evals/results/plain-language-ab-<ts>.json`` and ``.md``.

Isolation: the script removes every inherited ``PROTOAGENT_*`` variable before it
resolves anything. Run from inside a desktop-hosted agent, those variables point at
that agent's live workspace, and the arms would share (and the cleanup would
delete) its data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ── isolation first: nothing below may resolve paths under an inherited instance ──
for _k in [k for k in os.environ if k.startswith("PROTOAGENT_")]:
    os.environ.pop(_k, None)
for _k in ("A2A_PUBLIC_URL", "A2A_AUTH_TOKEN"):
    os.environ.pop(_k, None)

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent
_ROOT = _PLUGIN.parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evals import sweep  # noqa: E402
from evals.client import AgentClient  # noqa: E402
from graph.plugins.testkit import load_plugin  # noqa: E402

lint = load_plugin(_PLUGIN, "plain_language").lint

RESULTS = _ROOT / "evals" / "results"

JUDGE_SYSTEM = """You compare two replies to the same request and decide which one serves the reader better.

You will get the READER (who will read the reply), the REQUEST, and two replies.
Judge only this: which reply would this reader understand faster and more accurately, and be able to act on, without losing anything they need?
Count it against a reply if it leaves out or changes information the request gave or the reader needs, buries the main point, or uses words or structure this reader would struggle with.
Don't prefer a reply because it is longer or shorter, more formal or more casual, or uses more formatting. Length and formatting matter only as far as they help or hurt this reader.

Answer with JSON only, no prose around it:
{"better": "1" | "2" | "tie", "complete_1": true|false, "complete_2": true|false, "reason": "<one sentence>"}
complete_N is false when that reply drops or gets wrong something the reader needs."""


# ── arms ─────────────────────────────────────────────────────────────────────


def _seed_config(arm: str, ts: int) -> Path:
    """The bundled template plus this arm's plugin toggle.

    The template, not the operator's config, so both arms start exactly as a fresh
    agent does (the nightly sweep does the same); the model comes from ``--model``
    through ``PROTOAGENT_MODEL``."""
    from graph.config_io import config_example_path, load_yaml_doc, save_yaml_doc

    doc = load_yaml_doc(Path(config_example_path()))
    plugins = doc.get("plugins") if isinstance(doc.get("plugins"), dict) else {}
    enabled = [p for p in (plugins.get("enabled") or []) if p != "plain_language"]
    disabled = [p for p in (plugins.get("disabled") or []) if p != "plain_language"]
    if arm == "B":
        enabled.append("plain_language")
    else:
        disabled.append("plain_language")
    plugins["enabled"], plugins["disabled"] = enabled, disabled
    doc["plugins"] = plugins
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"plain-language-seed-{ts}-{arm}.yaml"
    save_yaml_doc(doc, out)
    return out


def _boot(arm: str, port: int, ts: int, token: str, model: str) -> tuple[subprocess.Popen, str, Path, str]:
    instance = f"plainlang-{arm.lower()}-{ts}"
    env = {
        **os.environ,
        "PROTOAGENT_INSTANCE": instance,
        "PROTOAGENT_UI": "none",
        "PROTOAGENT_SEED_CONFIG": str(_seed_config(arm, ts)),
        "PROTOAGENT_MODEL": model,
        "A2A_AUTH_TOKEN": token,
    }
    log_path = RESULTS / f"plain-language-server-{ts}-{arm}.log"
    log_f = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — closed with the process
    proc = subprocess.Popen(
        [sys.executable, "-m", "server", "--port", str(port), "--ui", "none"],
        cwd=str(_ROOT),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    proc._log_f = log_f  # type: ignore[attr-defined]
    return proc, f"http://127.0.0.1:{port}", log_path, instance


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        proc._log_f.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# ── generation ───────────────────────────────────────────────────────────────


async def _generate(client: AgentClient, cases: list[dict], repeat: int, concurrency: int, timeout_s: int) -> dict:
    sem = asyncio.Semaphore(concurrency)
    out: dict[tuple[str, int], dict] = {}

    async def one(case: dict, r: int) -> None:
        async with sem:
            res = await client.ask(case["prompt"], timeout_s=timeout_s)
            out[(case["id"], r)] = {
                "state": res.state,
                "text": res.text or "",
                "error": res.error,
                "duration_ms": res.duration_ms,
                "usage": res.usage,
            }
            print(
                f"    {case['id']} #{r + 1}: {res.state} ({res.duration_ms} ms, {len((res.text or '').split())} words)"
            )

    await asyncio.gather(*(one(c, r) for c in cases for r in range(repeat)))
    return out


# ── scoring ──────────────────────────────────────────────────────────────────


def fact_coverage(text: str, facts: list[str]) -> float:
    if not facts:
        return 1.0
    low = text.lower()
    hit = sum(1 for group in facts if any(alt.strip().lower() in low for alt in group.split("|")))
    return hit / len(facts)


def _parse_verdict(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {"better": "error", "reason": (raw or "")[:200]}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"better": "error", "reason": (raw or "")[:200]}
    better = str(data.get("better", "")).strip()
    if better not in ("1", "2", "tie"):
        better = "error"
    return {
        "better": better,
        "complete_1": bool(data.get("complete_1", True)),
        "complete_2": bool(data.get("complete_2", True)),
        "reason": str(data.get("reason", ""))[:400],
    }


def _make_judge(model: str | None):
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path
    from graph.llm import create_llm
    from graph.message_blocks import text_of
    from langchain_core.messages import HumanMessage, SystemMessage

    config = LangGraphConfig.from_yaml(str(config_yaml_path()))
    name = model or os.environ.get("EVAL_JUDGE_MODEL") or config.model_name
    llm = create_llm(config, model_name=name)

    async def judge(reader: str, request: str, first: str, second: str) -> dict:
        prompt = (
            f"READER: {reader}\n\nREQUEST:\n{request}\n\n"
            f"=== Response 1 ===\n{first[:12000]}\n\n=== Response 2 ===\n{second[:12000]}"
        )
        for attempt in range(3):
            try:
                resp = await llm.ainvoke([SystemMessage(JUDGE_SYSTEM), HumanMessage(prompt)])
                verdict = _parse_verdict(text_of(resp))
                if verdict["better"] != "error":
                    return verdict
            except Exception as exc:  # noqa: BLE001 — a judge hiccup is retried, then recorded
                verdict = {"better": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
            await asyncio.sleep(2 * (attempt + 1))
        return verdict

    return judge, name


def combine(v_ab: dict, v_ba: dict) -> int:
    """+1 if B wins, -1 if A wins, 0 for a tie or a disagreement.

    ``v_ab`` saw A as Response 1; ``v_ba`` saw B as Response 1."""

    def score(v: dict, b_slot: str) -> int:
        if v["better"] == b_slot:
            return 1
        if v["better"] in ("1", "2"):
            return -1
        return 0

    total = score(v_ab, "2") + score(v_ba, "1")
    return (total > 0) - (total < 0)


def sign_test_p(wins: int, losses: int) -> float:
    """Two-sided exact sign test (ties dropped)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = max(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / 2**n
    return min(1.0, 2 * tail)


async def _judge_all(cases, gen_a, gen_b, repeat, judge, concurrency) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    rows: list[dict] = []

    async def one(case: dict, r: int) -> None:
        a = gen_a.get((case["id"], r)) or {}
        b = gen_b.get((case["id"], r)) or {}
        ta, tb = a.get("text", ""), b.get("text", "")
        row = {"case": case["id"], "kind": case.get("kind", ""), "rep": r}
        if not ta.strip() or not tb.strip():
            row.update(outcome=None, note="missing reply", a_state=a.get("state"), b_state=b.get("state"))
            rows.append(row)
            return
        async with sem:
            v_ab, v_ba = await asyncio.gather(
                judge(case["reader"], case["prompt"], ta, tb),
                judge(case["reader"], case["prompt"], tb, ta),
            )
        ra, rb = lint.check(ta), lint.check(tb)
        row.update(
            outcome=combine(v_ab, v_ba),
            verdicts=[v_ab, v_ba],
            # complete_* per arm, from both orderings (an arm is complete only if both say so)
            a_complete=v_ab.get("complete_1", True) and v_ba.get("complete_2", True),
            b_complete=v_ab.get("complete_2", True) and v_ba.get("complete_1", True),
            a_facts=fact_coverage(ta, case.get("facts", [])),
            b_facts=fact_coverage(tb, case.get("facts", [])),
            a_lint=ra.per_100_words,
            b_lint=rb.per_100_words,
            a_words=ra.words,
            b_words=rb.words,
            a_ms=a.get("duration_ms", 0),
            b_ms=b.get("duration_ms", 0),
        )
        rows.append(row)
        mark = {1: "B", -1: "A", 0: "tie"}[row["outcome"]]
        print(
            f"    {case['id']} #{r + 1}: {mark}  lint {ra.per_100_words}→{rb.per_100_words}  words {ra.words}→{rb.words}"
        )

    await asyncio.gather(*(one(c, r) for c in cases for r in range(repeat)))
    rows.sort(key=lambda x: (x["case"], x["rep"]))
    return rows


# ── report ───────────────────────────────────────────────────────────────────


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 2) if xs else 0.0


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if r.get("outcome") is not None]
    wins = sum(1 for r in scored if r["outcome"] == 1)
    losses = sum(1 for r in scored if r["outcome"] == -1)
    ties = sum(1 for r in scored if r["outcome"] == 0)
    per_case: dict[str, dict] = {}
    for r in scored:
        c = per_case.setdefault(r["case"], {"B": 0, "A": 0, "tie": 0})
        c[{1: "B", -1: "A", 0: "tie"}[r["outcome"]]] += 1
    return {
        "pairs": len(rows),
        "scored": len(scored),
        "b_wins": wins,
        "a_wins": losses,
        "ties": ties,
        "b_win_rate_decided": round(wins / (wins + losses), 3) if wins + losses else None,
        "sign_test_p": round(sign_test_p(wins, losses), 4),
        "a_complete_rate": _mean([float(r["a_complete"]) for r in scored]),
        "b_complete_rate": _mean([float(r["b_complete"]) for r in scored]),
        "a_fact_coverage": _mean([r["a_facts"] for r in scored]),
        "b_fact_coverage": _mean([r["b_facts"] for r in scored]),
        "a_lint_per_100": _mean([r["a_lint"] for r in scored]),
        "b_lint_per_100": _mean([r["b_lint"] for r in scored]),
        "a_words": _mean([r["a_words"] for r in scored]),
        "b_words": _mean([r["b_words"] for r in scored]),
        "a_latency_ms": _mean([r["a_ms"] for r in scored]),
        "b_latency_ms": _mean([r["b_ms"] for r in scored]),
        "per_case": per_case,
    }


def render_md(meta: dict, s: dict, rows: list[dict]) -> str:
    lines = [
        f"# Plain-language A/B ({meta['ts_iso']})",
        "",
        f"Agent model: `{meta['agent_model']}` · judge: `{meta['judge_model']}` · "
        f"{meta['cases']} cases × {meta['repeat']} repeats",
        "",
        "| | A (baseline) | B (plain_language) |",
        "|---|---|---|",
        f"| Judge wins | {s['a_wins']} | {s['b_wins']} |",
        f"| Judge: complete | {s['a_complete_rate']:.0%} | {s['b_complete_rate']:.0%} |",
        f"| Required facts kept | {s['a_fact_coverage']:.0%} | {s['b_fact_coverage']:.0%} |",
        f"| Lint (weighted per 100 words, lower is plainer) | {s['a_lint_per_100']} | {s['b_lint_per_100']} |",
        f"| Mean words | {s['a_words']} | {s['b_words']} |",
        f"| Mean latency (ms) | {s['a_latency_ms']} | {s['b_latency_ms']} |",
        "",
        f"Ties or split verdicts: {s['ties']}. B win rate among decided pairs: "
        f"{s['b_win_rate_decided']} (two-sided sign test p = {s['sign_test_p']}).",
        "",
        "## Per case",
        "",
        "| Case | B wins | A wins | Ties |",
        "|---|---|---|---|",
    ]
    for case, c in sorted(s["per_case"].items()):
        lines.append(f"| {case} | {c['B']} | {c['A']} | {c['tie']} |")
    lines += ["", "## Judge reasons", ""]
    for r in rows:
        if r.get("outcome") is None:
            lines.append(f"- {r['case']} #{r['rep'] + 1}: not scored ({r.get('note')})")
            continue
        mark = {1: "B", -1: "A", 0: "tie"}[r["outcome"]]
        reasons = " / ".join(v.get("reason", "") for v in r["verdicts"])
        lines.append(f"- **{r['case']} #{r['rep'] + 1} → {mark}**: {reasons}")
    return "\n".join(lines) + "\n"


# ── main ─────────────────────────────────────────────────────────────────────


async def _run(args) -> int:
    cases = json.loads((_HERE / "prompts.json").read_text(encoding="utf-8"))["cases"]
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    ts = int(time.time())
    token = f"plainlang-ab-{ts}"
    os.environ["A2A_AUTH_TOKEN"] = token  # the client reads it too
    RESULTS.mkdir(parents=True, exist_ok=True)

    arms = {}
    try:
        for i, arm in enumerate(("A", "B")):
            proc, url, log_path, instance = _boot(arm, args.port_base + i, ts, token, args.model)
            arms[arm] = {"proc": proc, "url": url, "log": log_path, "instance": instance}
            print(f"  · arm {arm}: booting {url} (instance {instance})")
        health = {}
        for arm, a in arms.items():
            h = await asyncio.to_thread(sweep._wait_healthy, a["url"])
            if h is None:
                print(f"  ✗ arm {arm} never became healthy; see {a['log']}")
                return 1
            health[arm] = h
            print(f"  ✓ arm {arm} healthy (model={h.get('model')})")
        loaded_b = "plain_language" in arms["B"]["log"].read_text(encoding="utf-8", errors="replace")
        print(f"  · plain_language mentioned in arm B boot log: {loaded_b}")

        print("  — generating")
        gen = await asyncio.gather(
            *(
                _generate(AgentClient(base_url=arms[arm]["url"]), cases, args.repeat, args.concurrency, args.timeout)
                for arm in ("A", "B")
            )
        )
    finally:
        for arm, a in arms.items():
            _stop(a["proc"])
            if not args.keep:
                sweep._cleanup_instance(a["instance"])

    gen_a, gen_b = gen
    judge, judge_name = _make_judge(args.judge_model)
    print(f"  — judging with {judge_name}")
    rows = await _judge_all(cases, gen_a, gen_b, args.repeat, judge, args.concurrency)
    s = summarize(rows)
    meta = {
        "ts": ts,
        "ts_iso": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)),
        "agent_model": health["A"].get("model"),
        "judge_model": judge_name,
        "cases": len(cases),
        "repeat": args.repeat,
    }
    replies = {
        f"{cid}#{r + 1}": {"A": gen_a.get((cid, r)), "B": gen_b.get((cid, r))} for (cid, r) in sorted(gen_a.keys())
    }
    out_json = RESULTS / f"plain-language-ab-{ts}.json"
    out_md = RESULTS / f"plain-language-ab-{ts}.md"
    out_json.write_text(json.dumps({"meta": meta, "summary": s, "rows": rows, "replies": replies}, indent=2))
    out_md.write_text(render_md(meta, s, rows))
    print(render_md(meta, s, rows).split("## Per case")[0])
    print(f"  report: {out_md}\n  data:   {out_json}")
    return 0


def _default_model() -> str:
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path

    try:
        return LangGraphConfig.from_yaml(str(config_yaml_path())).model_name
    except Exception:  # noqa: BLE001
        return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repeat", type=int, default=2, help="replies per case per arm")
    p.add_argument("--cases", default=None, help="comma-separated case ids (default: all)")
    p.add_argument("--model", default=None, help="agent model for both arms (default: the default config's)")
    p.add_argument(
        "--judge-model", default=None, help="grader model (default: $EVAL_JUDGE_MODEL, then the config model)"
    )
    p.add_argument("--port-base", type=int, default=7996)
    p.add_argument("--concurrency", type=int, default=3, help="in-flight requests per arm")
    p.add_argument("--timeout", type=int, default=240, help="seconds per agent reply")
    p.add_argument("--keep", action="store_true", help="keep the throwaway instances")
    args = p.parse_args(argv)
    args.model = args.model or _default_model()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
