"""Content-free request/cache measurements for isolated prompt experiments.

Mount ``RequestProbe`` after provider shaping in an isolated graph's middleware
stack. It observes the effective LangChain request, NOT the provider's serialized
HTTP body or billing tokenizer. Reports contain lengths/hashes, never prompt text,
schema descriptions, arguments or credentials. Incognito calls are not recorded.
The caller owns the probe and decides where to save its report.
"""

from __future__ import annotations

import hashlib
import json
import time

from langchain.agents.middleware import AgentMiddleware
from langchain_core.utils.function_calling import convert_to_openai_tool

from graph.context_frame import is_context_frame
from graph.message_blocks import text_of, tool_calls_of
from observability.pricing import cache_tokens, reports_cache


def _json(value) -> str:
    # Preserve list AND mapping order: a sort here would conceal schema churn.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _fingerprint(text: str) -> dict:
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def request_shape(request) -> dict:
    """Attribute a request without exposing its content or counting tool args twice."""
    system = getattr(request, "system_message", None)
    settings = getattr(request, "model_settings", None) or {}
    instructions = settings.get("instructions")
    system_content = instructions if isinstance(instructions, str) else getattr(system, "content", "")
    tools = [convert_to_openai_tool(t) for t in (getattr(request, "tools", None) or [])]
    messages = []
    for m in getattr(request, "messages", None) or []:
        # Native tool_use blocks mirror tool_calls. The shared accessors own that
        # normalization, while content_hash preserves the original block shape.
        text = text_of(m)
        calls = tool_calls_of(m)
        messages.append({
            "role": m.type,
            "tool_name": getattr(m, "name", None) if m.type == "tool" else None,
            "projected": is_context_frame(m),
            "text": _fingerprint(text),
            "tool_args_chars": sum(len(_json(c.get("args", {}))) for c in calls),
            "content": _fingerprint(_json(m.content)),
            "cache_controls": [
                b["cache_control"] for b in m.content
                if isinstance(b, dict) and "cache_control" in b
            ] if isinstance(m.content, list) else [],
        })
    model = getattr(request, "model", None)
    return {
        "model": getattr(model, "model_name", None) or getattr(model, "model", "") or "",
        "system_delivery": "instructions" if isinstance(instructions, str) else "system_message",
        "system": _fingerprint(_json(system_content)),
        "system_blocks": [
            {**_fingerprint(b.get("text", "")), "cache_control": b.get("cache_control")}
            for b in system_content if isinstance(b, dict) and b.get("type") == "text"
        ] if isinstance(system_content, list) else [],
        "schemas": _fingerprint(_json(tools)),
        "tool_order": [t.get("function", {}).get("name", t.get("name", "")) for t in tools],
        "messages": messages,
    }


def usage_summary(response) -> dict:
    """Use normalized LangChain usage; unknown cache telemetry stays unknown."""
    usage = next((m.usage_metadata for m in (getattr(response, "result", None) or [])
                  if getattr(m, "usage_metadata", None)), None)
    if not usage:
        return {"reported": False, "cache_reported": False}
    details = usage.get("input_token_details") or {}
    cache_reported = reports_cache(details)
    read_reported = any(k == "cache_read" or k.endswith("_cache_read") for k in details)
    write_reported = any(k == "cache_creation" or k.endswith("_cache_creation") for k in details)
    read, write = cache_tokens(usage)
    total = int(usage.get("input_tokens") or 0)
    return {
        "reported": True,
        "input_tokens": total,
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_reported": cache_reported,
        "cache_read_tokens": read if read_reported else None,
        "cache_write_tokens": write if write_reported else None,
        # Includes cache creation: this is non-read input, not a billed-cost claim.
        "non_read_input_tokens": max(0, total - read) if read_reported else None,
        "cache_read_ratio": read / total if read_reported and total else None,
    }


class RequestProbe(AgentMiddleware):
    """Per-experiment observer, mounted after provider transforms.

    Rows are appended in completion order; use the caller's scenario label to
    group trials. Errors record only their type, never the provider's raw text.
    """

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.scenario = "unlabelled"

    async def awrap_model_call(self, request, handler):
        if (getattr(request, "state", None) or {}).get("incognito"):
            return await handler(request)
        row = {"scenario": self.scenario, **request_shape(request)}
        started = time.perf_counter()
        try:
            response = await handler(request)
        except Exception as exc:
            row["error_type"] = type(exc).__name__
            raise
        else:
            row["usage"] = usage_summary(response)
            return response
        finally:
            row["duration_ms"] = round((time.perf_counter() - started) * 1000)
            self.rows.append(row)

    def report(self) -> dict:
        return {
            "format_version": 1,
            "surface": "effective LangChain request after provider shaping; not raw HTTP",
            "ratio_denominator": "LangChain input_tokens, including cache reads and writes",
            "size_unit": "characters of compact JSON/text; not token counts or billing",
            "rows": self.rows,
        }


async def _live(config, model: str, repeat: int, probe=None) -> dict:
    """Run synthetic turns in a caller-isolated process using a configured model."""
    from unittest.mock import patch
    from pathlib import Path
    import tempfile

    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import MemorySaver

    import graph.agent as agent
    from graph.config import LangGraphConfig

    # Reuse only model connection settings, never production plugins, schedules,
    # goals, persona or stores. Model credentials remain in memory, not the report.
    cfg = LangGraphConfig.from_dict({
        "model": {"name": model, "max_tokens": 2048},
        "providers": [p.as_dict(redact=False) for p in config.providers],
        "middleware": {"memory": False, "audit": False},
        "compaction": {"enabled": False},
        "prompt_cache": {"enabled": True, "ttl": config.prompt_cache_ttl},
    })
    probe = probe if probe is not None else RequestProbe()
    shaping = agent.provider_shape_middleware
    outcomes = []
    with patch.object(agent, "provider_shape_middleware", lambda c: [*shaping(c), probe]):
        graph = agent.create_agent_graph(cfg, checkpointer=MemorySaver())
        sections = [{"label": label, "chars": len(text)} for label, text in graph.system_prompt_parts]
        for i in range(repeat):
            probe.scenario = f"identical-request-{i + 1}"
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content="Reply with exactly CACHE_BASELINE_OK. Do not call tools.")],
                 "session_id": f"baseline-{i}"},
                {"configurable": {"thread_id": f"baseline-{i}"}, "recursion_limit": 100},
            )
            outcomes.append({"scenario": probe.scenario, "passed": text_of(result["messages"][-1]).strip() == "CACHE_BASELINE_OK"})
        for i, prompt in enumerate([
            "Use calculator to compute 17 * 19. Reply with the number only.",
            "What was that result? Reply with the number only, without calling tools.",
        ]):
            probe.scenario = f"conversation-{i + 1}"
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content=prompt)], "session_id": "conversation"},
                {"configurable": {"thread_id": "conversation"}, "recursion_limit": 100},
            )
            correct = text_of(result["messages"][-1]).strip() == "323"
            called = any(getattr(m, "name", None) == "calculator" and m.type == "tool" for m in result["messages"])
            outcomes.append({"scenario": probe.scenario, "passed": correct and called})
        # A graph rebuild should preserve the prefix and schema order. This uses
        # a fresh thread so no history change is mistaken for reload churn.
        graph = agent.create_agent_graph(cfg, checkpointer=MemorySaver())
        probe.scenario = "reload-identical-request"
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="Reply with exactly CACHE_BASELINE_OK. Do not call tools.")],
             "session_id": "reload"},
            {"configurable": {"thread_id": "reload"}, "recursion_limit": 100},
        )
        outcomes.append({"scenario": probe.scenario, "passed": text_of(result["messages"][-1]).strip() == "CACHE_BASELINE_OK"})
        # Model-visible post-compaction fixture, not a claim that this runner
        # exercises the archival/summarization lifecycle (its tests do that).
        probe.scenario = "compacted-history-fixture"
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="Conversation summary: calculator returned 323 for 17 * 19.",
                                        additional_kwargs={"lc_source": "compaction"}),
                          HumanMessage(content="What was the calculator result? Reply with the number only.")],
             "session_id": "compacted"},
            {"configurable": {"thread_id": "compacted"}, "recursion_limit": 100},
        )
        outcomes.append({"scenario": probe.scenario, "passed": text_of(result["messages"][-1]).strip() == "323"})
        from knowledge.store import KnowledgeStore

        # Actual shared projection with synthetic hot memory. Rebuilding each
        # fixture also verifies that instance-store paths do not enter the prefix.
        for color in ("blue", "green"):
            with tempfile.TemporaryDirectory(prefix="pa-projection-fixture-") as folder:
                store = KnowledgeStore(Path(folder) / "kb.db")
                store.add_chunk(f"Today's fixture color is {color}.", domain="hot", heading="fixture")
                graph = agent.create_agent_graph(cfg, knowledge_store=store, checkpointer=MemorySaver())
                probe.scenario = f"projection-{color}"
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content="What is today's fixture color? Reply with one word, without tools.")],
                     "session_id": "projection"},
                    {"configurable": {"thread_id": "projection"}, "recursion_limit": 100},
                )
                outcomes.append({"scenario": probe.scenario, "passed": text_of(result["messages"][-1]).strip().lower() == color})
    return {**probe.report(), "model_target": model, "ttl": cfg.prompt_cache_ttl,
            "initial_stable_sections": sections, "quality": outcomes}


def main(argv=None) -> int:
    import argparse
    import asyncio
    import os
    import tempfile
    from pathlib import Path

    from graph.config import LangGraphConfig
    from infra.paths import reset_instance_paths

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="read-only source of configured model connections")
    parser.add_argument("--model", required=True, help="connection:model slot (live provider calls)")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180, help="whole-experiment deadline in seconds")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    config = LangGraphConfig.from_yaml(args.config)
    # CLI-only isolation: no server, background worker or production store is opened.
    previous = {k: os.environ.get(k) for k in ("PROTOAGENT_BOX_ROOT", "PROTOAGENT_HOME")}
    try:
        with tempfile.TemporaryDirectory(prefix="pa-prompt-baseline-") as root:
            os.environ.update(PROTOAGENT_BOX_ROOT=root, PROTOAGENT_HOME=root)
            reset_instance_paths()
            soul = Path(root) / "config" / "SOUL.md"
            soul.parent.mkdir()
            soul.write_text("# Identity\nI am an assistant running synthetic context and cache evaluations.\n")
            probe = RequestProbe()

            async def run():
                return await asyncio.wait_for(_live(config, args.model, max(1, args.repeat), probe), args.timeout)

            try:
                report = asyncio.run(run())
            except Exception as exc:
                report = {**probe.report(), "model_target": args.model, "failure_type": type(exc).__name__,
                          "quality": [{"scenario": probe.scenario, "passed": False}]}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(f"Recorded {len(report['rows'])} model calls to {args.output}")
            return 0 if all(x["passed"] for x in report["quality"]) else 1
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_instance_paths()


if __name__ == "__main__":
    raise SystemExit(main())
