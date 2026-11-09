"""On-demand memory retrieval comparison; no running agent or nightly gate."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from evals.memory_fixtures import load_fixture_store

FIXTURES = Path(__file__).resolve().parent / "memory_fixtures"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
SYSTEM_PROMPT = "You are a personalized AI assistant. Answer the question about the user based on your understanding of the user."


def topk(query, store, opts) -> list[dict]:
    """Use the runtime retrieval path; callers must run arms sequentially."""
    from graph.projection import _relevant_memory
    from runtime.state import STATE

    previous = STATE.memory_store
    try:
        STATE.memory_store = store
        return _relevant_memory(query, opts)
    finally:
        STATE.memory_store = previous


ARMS = {"topk": topk}


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cosine(a, b) -> float:
    if len(a) != len(b):
        raise ValueError("embedding dimensions differ")
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


def _memo_embed(embed_fn):
    """Cache embeddings by exact input string for one evaluate() call.

    The frozen fixture rows and the PV4 choices are embedded over and over —
    once per case, run and arm — so a single evaluate() otherwise fires tens of
    thousands of identical, serial gateway calls. Memoizing collapses each
    distinct text to one real embed. The cache lives only for the returned
    callable; nothing is written to disk. A ``None`` result (embeddings
    disabled or a gateway failure) passes straight through and is never cached,
    so later calls can still recover. Each hit returns a fresh ``list(vec)`` so
    a caller mutating the vector can't corrupt the cache.
    """
    cache: dict[str, list] = {}

    def embed(text):
        if text in cache:
            return list(cache[text])
        vec = embed_fn(text)
        if vec is None:
            return None
        cache[text] = list(vec)
        return list(vec)

    return embed


def score_pv4(case, rows, model, embed_fn) -> dict:
    """One answer call, then nearest non-empty candidate by cosine similarity."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from graph.message_blocks import text_of

    choices = [choice for choice in case["choices"] if choice and choice.strip()]
    if not choices or case["answer"] not in choices:
        raise ValueError(f"{case['id']}: reference must be a non-empty choice")
    response = model.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content="Memories:\n" + "\n".join(row.get("preview", row.get("content", "")) for row in rows)
                     + "\n\nQuestion: " + case["question"]),
    ])
    answer = text_of(response)
    vector = embed_fn(answer)
    similarities = [cosine(vector, embed_fn(choice)) for choice in choices]
    nearest = choices[max(range(len(choices)), key=similarities.__getitem__)]
    return {"passed": nearest == case["answer"], "response": answer,
            "nearest": nearest, "choices": choices, "similarities": similarities}


def seed_changed_fact(case, fixture, embed_fn):
    """Load a fresh copy with distractors, then consolidate in chronological order."""
    store = load_fixture_store(fixture, embed_fn)
    for content in case["memories"]:
        store.add(content, subject=case["subject"], kind=case["kind"],
                  provenance="operator", delivery="retrieved")
    return store


def score_changed_fact(case, rows) -> dict:
    contents = [row.get("preview", row.get("content", "")) for row in rows]
    newer = any(case["must_retrieve"] in content for content in contents)
    stale = any(case["must_not_retrieve"] in content for content in contents)
    return {"passed": newer and not stale, "newer_retrieved": newer, "stale_retrieved": stale}


def paired_table(results, arms, runs) -> list[dict]:
    """Compare strict-majority case outcomes (two of three for the default runs)."""
    if runs < 1:
        raise ValueError("runs must be positive")
    cases = sorted({row["case_id"] for row in results})
    counts = {(case, arm): 0 for case in cases for arm in arms}
    seen = set()
    for row in results:
        key = (row["case_id"], row["arm"], row["run"])
        if key in seen or not 1 <= row["run"] <= runs:
            raise ValueError("duplicate or invalid run")
        seen.add(key)
        counts[row["case_id"], row["arm"]] += bool(row["passed"])
    if len(seen) != len(cases) * len(arms) * runs:
        raise ValueError("paired comparison requires all case/arm/run results")
    table = []
    for a, b in combinations(arms, 2):
        wins_a = wins_b = ties = 0
        for case in cases:
            passed_a = counts[case, a] > runs / 2
            passed_b = counts[case, b] > runs / 2
            wins_a += passed_a and not passed_b
            wins_b += passed_b and not passed_a
            ties += passed_a == passed_b
        table.append({"a": a, "b": b, "a_wins": wins_a, "b_wins": wins_b, "ties": ties})
    return table


def evaluate(pv4, changed, fixture_dir, arms, runs, opts, model, embed_fn) -> dict:
    results = []
    if not pv4:
        raise ValueError("PV4 cases are required, including a distractor store")
    # One in-memory cache for the whole run: the same fixture rows and choices
    # are embedded on every case/run/arm, so memoize to one gateway call each.
    embed_fn = _memo_embed(embed_fn)
    distractor = fixture_dir / f"user_{pv4[0]['user_id']}.jsonl"
    for suite, cases in (("pv4", pv4), ("changed-fact", changed)):
        for case in cases:
            for run in range(1, runs + 1):
                for arm in arms:
                    # Fresh copies prevent an arm's mutations contaminating another run.
                    store = (load_fixture_store(fixture_dir / f"user_{case['user_id']}.jsonl", embed_fn)
                             if suite == "pv4" else seed_changed_fact(case, distractor, embed_fn))
                    rows = ARMS[arm](case["question"], store, opts)
                    score = (score_pv4(case, rows, model, embed_fn) if suite == "pv4"
                             else score_changed_fact(case, rows))
                    result = {"case_id": case["id"], "suite": suite, "run": run, "arm": arm,
                              "retrieved": rows, **score}
                    results.append(result)
                    print(f"{case['id']} run={run} arm={arm}: {'PASS' if score['passed'] else 'FAIL'}")
    return {"runs": runs, "arms": arms, "results": results,
            "paired": paired_table(results, arms, runs)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arms", default="topk")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--model", default=None)
    parser.add_argument("--json", dest="json_out", help="report filename within evals/results/")
    args = parser.parse_args(argv)
    arms = [name.strip() for name in args.arms.split(",")]
    if args.runs < 1 or len(set(arms)) != len(arms) or any(name not in ARMS for name in arms):
        parser.error("use positive --runs and distinct registered --arms: " + ",".join(ARMS))
    filename = args.json_out or datetime.now(timezone.utc).strftime("memory-retrieval-%Y%m%dT%H%M%S%fZ.json")
    if Path(filename).name != filename or filename in (".", ".."):
        parser.error("--json must be a filename within evals/results/")
    try:
        pv4 = load_cases(FIXTURES / "pv4" / "cases.jsonl")
        changed = load_cases(FIXTURES / "supersede_cases.jsonl")
        if not pv4:
            parser.error("PV4 cases are empty")
        for case in pv4:
            if not (FIXTURES / "pv4" / f"user_{case['user_id']}.jsonl").is_file():
                raise FileNotFoundError(f"missing store for user {case['user_id']}")
    except FileNotFoundError as exc:
        parser.error(f"{exc}. Generate frozen fixtures with python -m evals.memory_fixtures.build_pv4")

    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path
    from graph.llm import create_embed_fn, create_llm
    from graph.projection import ProjectionOptions

    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    embed_fn = create_embed_fn(cfg)
    if embed_fn is None:
        parser.error("configure knowledge.embed_model and gateway credentials")
    model = create_llm(cfg, model_name=args.model)
    opts = ProjectionOptions.from_config(cfg, model_name=args.model)
    report = evaluate(pv4, changed, FIXTURES / "pv4", arms, args.runs, opts, model, embed_fn)
    report["model"] = args.model or cfg.model_name
    print("\nA\tB\tA wins\tB wins\tTies")
    for row in report["paired"]:
        print(f"{row['a']}\t{row['b']}\t{row['a_wins']}\t{row['b_wins']}\t{row['ties']}")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output = RESULTS_DIR / filename
    output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
