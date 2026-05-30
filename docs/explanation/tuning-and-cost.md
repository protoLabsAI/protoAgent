# Tuning & cost

protoAgent ships the optimizations that long-running agent runtimes (Nous's
Hermes Agent, OpenClaw) treat as table stakes — context compaction, aux-model
routing, programmatic tool calling, prefix caching, and provider failover. Most
are config flags in `config/langgraph-config.yaml`. This page is the map.

## The levers

| Lever | Config | Default | What it buys |
|---|---|---|---|
| Context compaction | `compaction.*` | **on** | Summarize old turns near the window limit so long sessions don't overflow |
| Aux-model routing | `compaction.model`, `goal.eval_model` | main model | Run summarization + goal-verification on a cheaper/faster model |
| Programmatic tool calling | `execute_code.*` | off | One script composes many tools in a single turn |
| Prompt / prefix caching | `prompt_cache.*` | on (Anthropic-gated) | Cache the stable system+tools prefix across turns |
| Cache warming | `cache_warming.*` | off | Keep the cached prefix warm for sporadic, latency-sensitive traffic |
| Provider failover | `routing.fallback_models` | none | Retry on fallback models when the primary errors |
| Iteration budget | `model.max_iterations`, subagent `max_turns` | 50 / per-subagent | Stop runaway loops |

## Context compaction

`SummarizationMiddleware` summarizes the *middle* of the history when a trigger
fires, keeping the last `keep_messages` turns intact. On by default — a long
session would otherwise hit the context window and error.

```yaml
compaction:
  enabled: true
  trigger: "fraction:0.8"   # | "tokens:120000" | "messages:80"
  keep_messages: 20
  # model: protolabs/fast   # summarize with a cheap model (blank = main model)
```

**Trigger caveat:** `fraction:` and `tokens:` need the model's context-window
**profile**. A custom gateway alias (e.g. `protolabs/reasoning`) usually doesn't
expose one, so langchain raises at construction. The wiring catches this and
**falls back to a message-count trigger** (logged) rather than crashing the
graph — but for deterministic behavior on a profile-less model, set
`trigger: "messages:N"` explicitly.

## Aux-model routing

Not every model call is the hard reasoning task. Context summarization, goal
verification, and **subagent delegation** are lighter work — route them to a
cheaper, faster alias and reserve the reasoning model for the lead turn. One
knob covers all three:

```yaml
routing:
  aux_model: protolabs/fast
```

Each path resolves **specific override → `routing.aux_model` → main model**, so
you can still pin an individual path: `compaction.model`, `goal.eval_model`, or
a per-subagent `model` (in `graph/subagents/config.py`) — e.g. keep a
heavy-reasoning subagent on the main model while the rest run on the fast alias.

## Programmatic tool calling (`execute_code`)

Instead of a long `search → fetch → search → fetch` tool-call chain (one model
round-trip each), the model writes **one** Python script that calls several
tools, loops/filters/composes their results, and returns only stdout —
collapsing the chain into a single turn.

```yaml
execute_code:
  enabled: true
  timeout: 30
  tools: []   # empty = all tools except execute_code itself
```

::: warning Security
`execute_code` runs **model-authored code** in a subprocess with a scrubbed env
(no secrets) and a hard timeout, with tool calls bridged back over an fd-based
RPC. It's still arbitrary code execution — enable it only for a trusted model or
inside a hardened container, not on a workstation handling untrusted input.
:::

## Prefix caching & warming

`prompt_cache` applies Anthropic cache breakpoints to the stable system+tools
prefix; it's a safe no-op on non-Anthropic models (vLLM gateways do prefix
caching server-side). `cache_warming` reproduces the cached prefix on an
interval so the first request after an idle gap hits a warm cache — only worth
it for sporadic, latency-sensitive workloads on the `1h` tier; for steady
traffic it's pure cost.

## What's already optimal

- **Parallel tool calls** — langchain's `create_agent` runs a turn's tool calls
  concurrently; results are stitched back in order.
- **Interruptible turns** — A2A `tasks/cancel` + SSE disconnect stop a run
  cleanly without corrupting history.
- **Search-don't-load memory** — `KnowledgeMiddleware` retrieves top-k relevant
  memory rather than replaying full history into every prompt.
- **Self-authored skills** — successful subagent runs emit reusable `skill-v1`
  recipes, indexed in FTS5 and recalled into later prompts (the "closed learning
  loop").

## Related

- [Architecture](/explanation/architecture)
- [A2A protocol](/explanation/a2a-protocol) — streaming + cost-v1
- [Cost & trace propagation](/explanation/cost-and-trace)
- [Configuration reference](/reference/configuration)
