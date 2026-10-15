"""Synthetic catalog comparison: full, deferred and domain-owned tools.

Uses actual lead and in-graph worker requests, not raw HTTP. Synthetic worker
requests are identified by their catalog-only schema; both scopes are reported. No production
stores, plugins or OAuth stores are used. Reports contain unkeyed content digests.
"""
import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver

from evals.prompt_context import RequestProbe
from graph.config import LangGraphConfig
from graph.message_blocks import text_of
from graph.subagents.config import SubagentConfig, SUBAGENT_REGISTRY
from infra.paths import instance_paths, reset_instance_paths


def catalog(size=40):
    tools = []
    for i in range(size):
        def make_lookup(index):
            def lookup() -> str:
                return f"catalog-value-{index}-cobalt"
            return lookup
        tools.append(StructuredTool.from_function(make_lookup(i), name=f"fixture_{i}",
            description=f"Read synthetic catalog record {i}. Returns its current exact value; no arguments."))
    return tools


def invoked_this_turn(messages, expected_names):
    last_input = max((i for i, m in enumerate(messages) if m.type == "human"), default=-1)
    return any(m.type == "tool" and getattr(m, "name", None) in expected_names
               for m in messages[last_input + 1:])


async def experiment(source, model, repeat, probe, outcomes):
    import graph.agent as agent
    tools = catalog()
    role = SubagentConfig(name="fixture_catalog", description="Reads synthetic catalog records fixture_0 through fixture_39.",
                         system_prompt="Read the requested catalog record with its tool and return its exact value.",
                         tools=[t.name for t in tools], max_turns=5)
    shaping = agent.provider_shape_middleware
    with patch.dict(SUBAGENT_REGISTRY, {}), patch.object(
        agent, "provider_shape_middleware", lambda c: [*shaping(c), probe]
    ):
        for policy in ("full", "deferred", "domain"):
            if policy == "domain":
                SUBAGENT_REGISTRY[role.name] = role
            else:
                SUBAGENT_REGISTRY.pop(role.name, None)
            cfg = LangGraphConfig.from_dict({
                "model": {"name": model, "max_tokens": 2048},
                "providers": [p.as_dict(redact=False) for p in source.providers],
                "middleware": {"memory": False, "audit": False},
                "compaction": {"enabled": False},
                "prompt_cache": {"enabled": True, "ttl": source.prompt_cache_ttl},
                "tools": {"deferred": {"enabled": policy == "deferred"},
                          "subagent_only": [t.name for t in tools] if policy == "domain" else []},
            })
            for trial in range(repeat):
                graph = agent.create_agent_graph(cfg, extra_tools=tools, checkpointer=MemorySaver())
                # Repeated requests, a second discovery and a fresh post-compaction
                # fixture exercise both schema widening and rediscovery.
                for step, index in enumerate((17, 17, 23, 17)):
                    probe.scenario = f"{policy}/{trial + 1}/{step + 1}"
                    sid = f"{policy}-{trial}" if step < 3 else f"{policy}-{trial}-compacted"
                    prompt = f"Use fixture_{index} to read its current value. "
                    prompt += ("Delegate to fixture_catalog. " if policy == "domain" else "Discover the tool if needed. Do not delegate. ")
                    prompt += "Return only the exact value."
                    messages = [HumanMessage(content=prompt)]
                    if step == 3:
                        messages.insert(0, HumanMessage(content="Summary: earlier catalog work finished. Verify the current value again.",
                                                       additional_kwargs={"lc_source": "compaction"}))
                    started = time.perf_counter()
                    result = await asyncio.wait_for(graph.ainvoke(
                        {"messages": messages, "session_id": sid},
                        {"configurable": {"thread_id": sid}, "recursion_limit": 100}), 180)
                    answer = text_of(result["messages"][-1]).strip()
                    names = {"task", "task_batch"} if policy == "domain" else {f"fixture_{index}"}
                    called = invoked_this_turn(result["messages"], names)
                    if policy == "domain":
                        called = called and any(r["scenario"] == probe.scenario and
                            any(m["tool_name"] == f"fixture_{index}" for m in r["messages"] if m["role"] == "tool")
                            for r in probe.rows)
                    outcomes.append({"scenario": probe.scenario, "passed": answer == f"catalog-value-{index}-cobalt" and called,
                                     "tool_invoked": called, "elapsed_ms": round((time.perf_counter() - started) * 1000)})


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
        with tempfile.TemporaryDirectory(prefix="pa-tool-eval-") as root:
            os.environ.update(PROTOAGENT_BOX_ROOT=root, PROTOAGENT_HOME=root)
            reset_instance_paths()
            soul = instance_paths().soul_path
            soul.parent.mkdir(parents=True, exist_ok=True)
            soul.write_text("# Identity\nAn assistant evaluating synthetic catalog tools.\n")
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
    for row in probe.rows:
        row["scope"] = "worker" if row["tool_order"] and all(n.startswith("fixture_") for n in row["tool_order"]) else "lead"
    report = {**probe.report(), "model_target": args.model, "quality": outcomes, "failure_type": failure,
              "cost_scope": "lead plus in-graph catalog worker calls; no external workers"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Recorded {len(probe.rows)} model calls and {len(outcomes)} outcomes to {args.output}")
    return 1 if failure or not outcomes or not all(x["passed"] for x in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
