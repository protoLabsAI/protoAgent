# Configure subagents

Subagents are specialized LLM workers the lead agent delegates to via the `task()` tool. The template ships one worked example: a `researcher` (web + memory, plan→search→synthesize→cite). This guide walks through adding more, trimming down, or turning the pattern off entirely.

## When to use subagents

- You have clearly separable phases in your agent's work (e.g. *research*, *synthesize*, *publish*).
- You want each phase to get its own focused system prompt and tool allowlist.
- You want each phase's tool calls audited + traced under the same session as the lead.

When *not* to use:

- For a single-loop agent. Adding a subagent hop for every call just wastes turns.
- When one delegation's output feeds the next — use sequential `task()` calls (or a chain) for that. For *independent* delegations, see `task_batch` below.

## Single vs. batch delegation

The lead gets two delegation tools:

- **`task(description, prompt, subagent_type, emit_skill)`** — one focused delegation. Unbounded output.
- **`task_batch(tasks)`** — several *independent* delegations run **concurrently** (e.g. research three topics at once). Each `tasks` item is `{description, prompt, subagent_type?, emit_skill?}`. Results come back in input order; an individual task's failure is reported inline and doesn't abort the batch. Concurrency is capped by `subagents.max_concurrency` (default 4) and each result is truncated to `subagents.output_truncate` chars (default 6000) so a wide fan-out can't blow the parent context. Total latency is roughly the slowest task rather than the sum.

Prefer `task_batch` whenever the delegations don't depend on each other.

## 1. Define the config

`graph/subagents/config.py` already defines `RESEARCHER_CONFIG`. To add a second role, define another `SubagentConfig` and register it:

```python
SUMMARIZER_CONFIG = SubagentConfig(
    name="summarizer",
    description=(
        "Condenses long source text into a tight brief. "
        "Returns a ≤200-word summary; the lead decides what to do next."
    ),
    system_prompt="""You are the summarizer subagent.

Your job: given source text or URLs, return a concise brief (≤200 words):
- What the material says
- Key facts worth keeping
- Any obvious gaps or caveats

Rules:
- Keep responses focused — the lead agent is waiting on your return
  value, not a conversation.
- Use the same <scratch_pad> / <output> format as the lead agent.
""",
    tools=["fetch_url", "current_time"],
    max_turns=15,
)

SUBAGENT_REGISTRY: dict[str, SubagentConfig] = {
    "researcher": RESEARCHER_CONFIG,
    "summarizer": SUMMARIZER_CONFIG,
}
```

## 2. Expose the config shape

The template's `LangGraphConfig` (in `graph/config.py`) has a `researcher` field. Add one for each new subagent:

```python
@dataclass
class LangGraphConfig:
    # ... existing fields ...
    researcher: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=[
            "current_time",
            "web_search", "fetch_url",
            "memory_recall", "memory_list",
        ],
        max_turns=40,
    ))
    summarizer: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=["fetch_url", "current_time"],
        max_turns=15,
    ))
```

And update the `from_yaml()` subagent loop:

```python
for name in ("researcher", "summarizer"):  # ← add new names
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
  researcher:
    enabled: true
    tools:
      - current_time
      - web_search
      - fetch_url
      - memory_recall
      - memory_list
    max_turns: 40
  summarizer:
    enabled: true
    tools: [fetch_url, current_time]
    max_turns: 15
```

## 4. Teach the lead agent

The lead's `task()` tool docstring is how the LLM learns what subagents exist. It's generated automatically from `SUBAGENT_REGISTRY`, but the lead also needs to *know when to delegate*. Update `graph/prompts.py::build_system_prompt`:

```python
SYSTEM_PROMPT = """You are my-agent.

Available subagents (invoke via the `task` tool):
- `researcher` — gathers + synthesizes background on a topic, returns a sourced brief
- `summarizer` — condenses long source text into a ≤200-word brief

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

Neither `task` nor `task_batch` is ever in a subagent's tool allowlist (subagents only get the tools named in their `tools:` list), so subagents can't spawn further subagents. This is intentional; one level of delegation is almost always enough.

## Related

- [Architecture explanation](/explanation/architecture) — how the task tool fits into the LangGraph runtime
- [Starter tools reference](/reference/starter-tools) — which tool names you can add to an allowlist
