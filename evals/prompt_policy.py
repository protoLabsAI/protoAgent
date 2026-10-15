"""Isolated continuity-policy comparison using shipped fixtures and request probes.

Live calls only to configured model connections. No production stores, server,
plugins or OAuth stores are reused. Reports contain synthetic fixture answers and
unkeyed request digests: treat them as sensitive metadata, not anonymized data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver

from evals import judge, verify
from evals.prompt_context import RequestProbe
from graph.config import LangGraphConfig
from graph.message_blocks import text_of
from infra.paths import instance_paths, reset_instance_paths


def continuity_cases():
    tasks = json.loads(Path(__file__).with_name("tasks.json").read_text())
    names = {"continuity_direct_recall", "continuity_vague_recall", "continuity_update",
             "continuity_unrelated_noise", "continuity_abstention"}
    # Keep all continuity cases except transfer/tool-specific acceptance tests.
    return [t for t in tasks if t["id"] in names]


def check_output(case, answer, messages):
    names = {getattr(m, "name", "") for m in messages if m.type == "tool"}
    return (all(re.search(p, answer, re.I) for p in case.get("expected_patterns", []))
            and not any(re.search(p, answer, re.I) for p in case.get("forbidden_patterns", []))
            and not names.intersection(case.get("forbidden_tools", [])))


async def experiment(source, model, repeat, probe, outcomes):
    import graph.agent as agent
    from graph.llm import create_llm
    from knowledge.store import KnowledgeStore

    cases = continuity_cases()
    for case in cases:
        error = verify.apply_setup(case.get("setup", []), memory_dir=str(instance_paths().store("memory")))
        if error:
            raise RuntimeError("synthetic fixture seeding failed")
    cfg_data = {"model": {"name": model, "max_tokens": 2048},
                "providers": [p.as_dict(redact=False) for p in source.providers],
                "middleware": {"memory": False, "audit": False},
                "compaction": {"enabled": False},
                "prompt_cache": {"enabled": True, "ttl": source.prompt_cache_ttl}}
    shaping = agent.provider_shape_middleware
    with patch.object(agent, "provider_shape_middleware", lambda c: [*shaping(c), probe]):
        for policy in ("newest", "relevant", "off"):
            cfg = LangGraphConfig.from_dict({**cfg_data, "context": {"prior_sessions": policy}})
            store = KnowledgeStore(instance_paths().store("policy-fixture.db"))
            graph = agent.create_agent_graph(cfg, knowledge_store=store, checkpointer=MemorySaver())
            grader = create_llm(cfg, model_name=model)
            for trial in range(repeat):
                for case in cases:
                    probe.scenario = f"{policy}/{case['id']}/{trial + 1}"
                    sid = f"policy-{policy}-{case['id']}-{trial}"
                    result = await asyncio.wait_for(graph.ainvoke(
                        {"messages": [HumanMessage(content=case["prompt"])], "session_id": sid},
                        {"configurable": {"thread_id": sid}, "recursion_limit": 100}), 120)
                    answer = text_of(result["messages"][-1])
                    passed = check_output(case, answer, result["messages"])
                    rubric = case.get("verify_rubric")
                    score = None
                    if rubric:
                        # Grader calls are deliberately outside the agent probe and
                        # excluded from task input/cache/latency comparisons.
                        response = await asyncio.wait_for(grader.ainvoke([
                            SystemMessage(content=judge._GRADER_SYSTEM),
                            HumanMessage(content=judge._build_prompt(answer, rubric["criteria"]))]), 60)
                        verdict = judge._parse(text_of(response), rubric["criteria"])
                        score = verdict.score
                        passed = passed and not verdict.error and score >= rubric["threshold"]
                    outcomes.append({"scenario": probe.scenario, "passed": bool(passed),
                                     "rubric_score": score, "synthetic_answer": answer})


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--model", required=True)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args(argv)
    source = LangGraphConfig.from_yaml(args.config)
    previous = {k: os.environ.get(k) for k in ("PROTOAGENT_BOX_ROOT", "PROTOAGENT_HOME")}
    probe, outcomes = RequestProbe(), []
    failure = None
    try:
        with tempfile.TemporaryDirectory(prefix="pa-policy-eval-") as root:
            os.environ.update(PROTOAGENT_BOX_ROOT=root, PROTOAGENT_HOME=root)
            reset_instance_paths()
            soul = instance_paths().soul_path
            soul.parent.mkdir(parents=True, exist_ok=True)
            soul.write_text("# Identity\nAn assistant evaluating synthetic continuity fixtures.\n")
            try:
                asyncio.run(experiment(source, args.model, max(1, args.repeat), probe, outcomes))
            except Exception as exc:
                failure = type(exc).__name__
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_instance_paths()
    report = {**probe.report(), "model_target": args.model, "quality": outcomes, "failure_type": failure}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Recorded {len(probe.rows)} agent calls and {len(outcomes)} outcomes to {args.output}")
    return 1 if failure or not outcomes or not all(x["passed"] for x in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
