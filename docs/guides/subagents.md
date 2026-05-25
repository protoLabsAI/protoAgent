# Configure subagents

Subagents are specialized LLM workers the lead agent delegates to via the `task()` tool. The template ships with one placeholder `worker`. This guide walks through either adding more, trimming down, or turning the pattern off entirely.

## When to use subagents

- You have clearly separable phases in your agent's work (e.g. *research*, *synthesize*, *publish*).
- You want each phase to get its own focused system prompt and tool allowlist.
- You want each phase's tool calls audited + traced under the same session as the lead.

When *not* to use:

- For a single-loop agent. Adding a subagent hop for every call just wastes turns.
- For parallel fan-out. `task()` is sequential; if you need parallelism, spawn from your tool code directly.

## 1. Define the config

Edit `graph/subagents/config.py`:

```python
RESEARCHER_CONFIG = SubagentConfig(
    name="researcher",
    description=(
        "Gathers background on a topic via web_search + fetch_url. "
        "Returns a 200-word brief; the lead decides what to do next."
    ),
    system_prompt="""You are the researcher subagent.

Your job: given a topic, search the web, read the top few results,
and return a concise brief (≤200 words) that covers:
- What the topic is
- Key facts worth knowing
- Any obvious risks or controversies

Rules:
- Keep responses focused — the lead agent is waiting on your return
  value, not a conversation.
- Use the same <scratch_pad> / <output> format as the lead agent.
""",
    tools=["web_search", "fetch_url", "current_time"],
    max_turns=15,
)

SUBAGENT_REGISTRY: dict[str, SubagentConfig] = {
    "worker": WORKER_CONFIG,
    "researcher": RESEARCHER_CONFIG,
}
```

## 2. Expose the config shape

The template's `LangGraphConfig` (in `graph/config.py`) has a `worker` field. Add one for each new subagent:

```python
@dataclass
class LangGraphConfig:
    # ... existing fields ...
    worker: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=[
            "current_time", "calculator", "web_search", "fetch_url",
            "memory_ingest", "memory_recall", "memory_list", "memory_stats",
            "daily_log",
        ],
        max_turns=20,
    ))
    researcher: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=["web_search", "fetch_url", "current_time"],
        max_turns=15,
    ))
```

And update the `from_yaml()` subagent loop:

```python
for name in ("worker", "researcher"):  # ← add new names
    if name in subagents:
        sub = subagents[name]
        setattr(config, name, SubagentDef(
            enabled=sub.get("enabled", True),
            tools=sub.get("tools", getattr(config, name).tools),
            max_turns=sub.get("max_turns", getattr(config, name).max_turns),
        ))
```

## 3. Add to the YAML

`config/langgraph-config.yaml`:

```yaml
subagents:
  worker:
    enabled: true
    tools:
      - current_time
      - calculator
      - web_search
      - fetch_url
      - memory_ingest
      - memory_recall
      - memory_list
      - memory_stats
      - daily_log
    max_turns: 20
  researcher:
    enabled: true
    tools: [web_search, fetch_url, current_time]
    max_turns: 15
```

## 4. Teach the lead agent

The lead's `task()` tool docstring is how the LLM learns what subagents exist. It's generated automatically from `SUBAGENT_REGISTRY`, but the lead also needs to *know when to delegate*. Update `graph/prompts.py::build_system_prompt`:

```python
SYSTEM_PROMPT = """You are my-agent.

Available subagents (invoke via the `task` tool):
- `researcher` — gathers background on a topic, returns a ≤200-word brief
- `worker` — general-purpose tool runner

Delegate to researcher when a user asks an open-ended "find out about X"
question. Handle short factual queries yourself.
"""
```

## 5. Turn subagents off entirely

If your agent is simple enough that subagents are pure overhead, flip `include_subagents=False` when the graph is built. In `server.py::_init_langgraph_agent`:

```python
_graph = create_agent_graph(
    _graph_config,
    knowledge_store=knowledge_store,  # keep the bundled store wired up
    include_subagents=False,           # ← skip the task() tool and subagent machinery
)
```

This drops the `task()` tool from the lead's toolset. No runtime hit.

## What you get for free

Every subagent call:

- Runs inside the same `trace_session` context as the lead → nested Langfuse span.
- Inherits the same `session_id` → audit-log entries from the subagent's tools land alongside the lead's.
- Emits the same `autonomous.cost.*` events on terminal completion.
- Is rate-limited by `max_turns` (hard stop — avoids runaway recursion).

`task` is on the `disallowed_tools` list by default, so subagents can't spawn further subagents. This is intentional; one level of delegation is almost always enough.

## Related

- [Architecture explanation](/explanation/architecture) — how the task tool fits into the LangGraph runtime
- [Starter tools reference](/reference/starter-tools) — which tool names you can add to an allowlist
