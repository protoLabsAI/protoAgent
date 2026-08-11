# Run on a coding agent (ACP runtime) — deprecated

::: warning Deprecated — use a coding-agent delegate instead
Running the **whole turn** on an external coding agent is **no longer offered**. The option is
gone from the setup wizard and Settings; new instances can't select it.

- **Existing configs keep working.** An `agent_runtime: acp:<agent>` already in your YAML still
  drives turns, and the console labels it deprecated. Nothing breaks on upgrade.
- **The supported way to use a coding agent is a delegate** — [`delegate_to`](/guides/delegates)
  with an `acp` delegate. Your agent hands a coding job to protoCLI / Claude Code / Codex and
  gets the result back, keeping protoAgent's own loop (and its memory, goals, and tool
  policy) in charge of the turn.
- **To switch off it**, pick any brain in Settings ▸ Model (a gateway model, or your Claude /
  ChatGPT subscription) — the save rewrites `agent_runtime` to `native`.

The rest of this page documents the mode as it still behaves, for instances that run it.
:::

## What it does

protoAgent normally runs its turns on the built-in **LangGraph** loop. In this mode it hands
the whole turn to an **external coding agent** — **proto, Codex, Claude, Copilot, OpenCode** —
over the [Agent Client Protocol (ACP)](https://agentclientprotocol.com). The coding agent
becomes the *brain* (it reasons and uses its own tools); protoAgent stays the *shell* — A2A
endpoint, scheduling, goals, console, memory — wrapped around it.

> This is the **inverse** of a [coding-agent delegate](/guides/coding-agents): there your agent
> *calls out* to a coding agent as one step of its own turn — which is why the delegate is the
> pattern that survived. Here the coding agent *replaces* the runtime.

See [ADR 0033](/adr/0033-pluggable-agent-runtime-acp) for the original design.

## Configure it (existing instances)

There is no UI for these keys any more — edit the YAML directly.

```yaml
agent_runtime: acp:proto         # native (default) | acp:<agent>

# By default the coding agent gets protoAgent's FULL toolset — parity with the native
# runtime, where the model has every tool. `operator_mcp.tools` is an OPTIONAL restriction:
# list specific tools to clamp the brain, or omit it entirely for everything. (`execute_code`
# is excluded from the default set — the coding agent has its own; add it by name to override.)
# operator_mcp:
#   tools: [memory_recall, memory_ingest, task_create, task_list, notes_read, run_workflow]

# Optional — override an agent's launch command (defaults shown).
# acp:
#   agents:
#     proto:    { command: proto,  args: ["--acp"] }
#     codex:    { command: npx,    args: ["-y", "@agentclientprotocol/codex-acp"] }
#     claude:   { command: npx,    args: ["-y", "@agentclientprotocol/claude-agent-acp"] }
#     opencode: { command: opencode, args: ["acp"] }
```

Each agent needs its CLI **installed + authenticated** on the host. Defaults are best-effort
(ACP servers move) — override the `command`/`args` if yours differs.

## How a turn runs

1. **Persona** — your `SOUL.md` is written as **`AGENTS.md`** (plus the agent's own canonical
   file where it differs — `CLAUDE.md`, `GEMINI.md`, or `.github/copilot-instructions.md` for
   Copilot) into the session's working dir, which the coding agent loads into **its own** system
   prompt — so
   it adopts *your* agent's identity instead of its built-in "I'm Codex/Claude" default. (Ask it
   "who are you?" — it answers as your agent.) The session runs in a dedicated, instance-scoped
   workspace, not your repo, so it never touches your project's own `AGENTS.md`.
2. **Context** — each turn carries only the per-turn delta (retrieved knowledge + the always-on
   `<available_skills>` index; the brain loads a skill's full body on demand via the `load_skill`
   operator tool, ADR 0060) + your message. ACP sessions are stateful, so the agent keeps history —
   we don't resend the world each turn, which keeps the agent's own prompt caching intact.
3. **Tools** — protoAgent's operator tools are published as an MCP server (see
   [MCP → Expose this agent](/guides/mcp#expose-this-agent-as-an-mcp-server)) and **mounted into
   the ACP session** (`session/new` `mcpServers`). The coding agent calls `task_create`,
   `memory_recall`, `run_workflow`, … alongside its own tools. As it works, its tool calls stream
   to the chat as **tool cards** (`tool_start`/`tool_end`), the same as the native runtime.
4. **Drive** — the agent reasons + acts; protoAgent returns the result on its A2A/chat surface.
   The chat's model indicator shows the active runtime (`<agent> · coding agent`) rather than
   the gateway model, since the gateway model never runs the turn.
5. **Write back** — durable facts persist to the knowledge store after the turn.

One stateful ACP session is kept **per conversation thread** and reused across turns.

> **Prefer protoAgent's tools for state.** A coding agent has its *own* todo/memory tools
> (e.g. proto's `TaskCreate`) and will reach for them by default — state that then vanishes
> with its session. The persona file steers it to use the `protoagent-operator` tools
> (`task_create`, `memory_ingest`, `set_goal`, …) for anything that must **persist** in
> protoAgent — and they're available **by default** (the full toolset rides the bus; clamp
> it with `operator_mcp.tools` only if you want to restrict the brain).

## No gateway? ACP-only works

If your runtime is `acp:<agent>` and you have **no** OpenAI-compatible gateway key configured,
protoAgent's own auxiliary LLM calls (compaction, goal verification, fact extraction) **fall
back to the same coding agent** — so you can run entirely on e.g. your Claude/Codex login with
no separate model endpoint. (Embeddings are a separate axis: without an embed endpoint, semantic
recall degrades to keyword search.)

## What reaches the coding agent

| Capability | How |
|---|---|
| Tools (core + plugin) | the operator **MCP bus** — the **full toolset by default** (clamp with `operator_mcp.tools`); plugins ride it for free |
| Subagents / workflows | as tools (`task`, `run_workflow`) on the bus |
| Knowledge / memory | R/W via tools on the bus; **auto-recall** injected as context |
| Skills, SOUL/persona, history | **context** (assembled into the prompt) |
| MCP-server plugins (e.g. Google) | the coding agent mounts them directly |

## Security

By default the coding agent gets protoAgent's **full toolset** — parity with the native
runtime, where the model has every tool. To clamp a specific instance, set `operator_mcp.tools`
to a named allowlist (`execute_code` is already excluded from the default set — the coding
agent has its own; add it by name only if you mean to). Note the *foreign* MCP clients of this
same operator server (Claude Desktop, Cursor) stay allowlist-gated — it's the ACP **brain** that
defaults to all. The agent runs with its own permissions on the host (its CLI's auth + sandbox).

## Limits

- The native and ACP runtimes don't run in the same turn — `agent_runtime` picks one.
- **Deprecated**: no new selection path exists (wizard + Settings dropped it), so this mode
  only persists where it was already configured. A [coding-agent delegate](/guides/delegates)
  is the supported replacement and composes with everything else the native loop does.
- The agent's answer **streams** as it emits text chunks (and tool calls render as cards in
  order). Granularity is the agent's — proto sends a few coarse chunks rather than per-token;
  agents that stream token-by-token render finer.
- Instances run from the **same directory** share a derived workspace; give each an explicit
  `PROTOAGENT_INSTANCE` if you run several on one box (see [Run multiple instances](/guides/multi-instance)).
- Validate live — a real coding agent's behavior (and ACP version) is the true test; CI mocks it.
