# Run on a coding agent (ACP runtime)

protoAgent normally runs its turns on the built-in **LangGraph** loop. It can instead hand
the whole turn to an **external coding agent** — **proto, Codex, Claude, Copilot, OpenCode**
— over the [Agent Client Protocol (ACP)](https://agentclientprotocol.com). The coding agent
becomes the *brain* (it reasons and uses its own tools); protoAgent stays the *shell* — A2A
endpoint, scheduling, goals, console, memory — wrapped around it.

> This is the **inverse** of [Spawn CLI coding agents](/guides/coding-agents): there the agent
> *calls out* to a coding agent as a tool; here a coding agent *drives the runtime*.

It's **opt-in** — the default runtime is `native`, so nothing changes until you set it.
See [ADR 0033](/adr/0033-pluggable-agent-runtime-acp) for the design.

## Why

- Run the agent on the model + subscription you already use (e.g. your Claude or Codex login).
- Get the coding agent's full native toolset (file edit, shell) *inside* protoAgent's operable,
  schedulable, goal-driven A2A runtime.
- Swap brains by config — the runtime is a separate axis from the model reference.

## Enable it

```yaml
agent_runtime: acp:proto         # native (default) | acp:<agent>

# Expose the operator tools the coding agent may use (allowlist — empty = none).
operator_mcp:
  tools: [memory_recall, memory_ingest, beads_create, beads_list, notes_read, run_workflow]

# Optional — override an agent's launch command (defaults shown).
# acp:
#   agents:
#     proto:    { command: proto,  args: ["--acp"] }
#     codex:    { command: npx,    args: ["-y", "@zed-industries/codex-acp"] }
#     claude:   { command: npx,    args: ["-y", "@agentclientprotocol/claude-agent-acp"] }
#     opencode: { command: opencode, args: ["acp"] }
```

Each agent needs its CLI **installed + authenticated** on the host. Defaults are best-effort
(ACP servers move) — override the `command`/`args` if yours differs.

## How a turn runs

1. **Persona** — your `SOUL.md` is written as **`AGENTS.md`** (plus a vendor file like `CLAUDE.md`)
   into the session's working dir, which the coding agent loads into **its own** system prompt — so
   it adopts *your* agent's identity instead of its built-in "I'm Codex/Claude" default. (Ask it
   "who are you?" — it answers as your agent.) The session runs in a dedicated, instance-scoped
   workspace, not your repo, so it never touches your project's own `AGENTS.md`.
2. **Context** — each turn carries only the per-turn delta (retrieved knowledge / skills) + your
   message. ACP sessions are stateful, so the agent keeps history — we don't resend the world each
   turn, which keeps the agent's own prompt caching intact.
3. **Tools** — protoAgent's operator tools are published as an MCP server (see
   [MCP → Expose this agent](/guides/mcp#expose-this-agent-as-an-mcp-server)) and **mounted into
   the ACP session** (`session/new` `mcpServers`). The coding agent calls `beads_create`,
   `memory_recall`, `run_workflow`, … alongside its own tools.
4. **Drive** — the agent reasons + acts; protoAgent returns the result on its A2A/chat surface.
5. **Write back** — durable facts persist to the knowledge store after the turn.

One stateful ACP session is kept **per conversation thread** and reused across turns.

## What reaches the coding agent

| Capability | How |
|---|---|
| Tools (core + plugin) | the operator **MCP bus** — allowlisted, plugins ride it for free |
| Subagents / workflows | as tools (`task`, `run_workflow`) on the bus |
| Knowledge / memory | R/W via tools on the bus; **auto-recall** injected as context |
| Skills, SOUL/persona, history | **context** (assembled into the prompt) |
| MCP-server plugins (e.g. Google) | the coding agent mounts them directly |

## Security

The coding agent only gets the tools you **allowlist** in `operator_mcp.tools` — nothing by
default. Don't expose powerful tools (e.g. `execute_code`) to an external brain unless you mean
to. The agent runs with its own permissions on the host (its CLI's auth + sandbox).

## Limits

- The native and ACP runtimes don't run in the same turn — `agent_runtime` picks one.
- Token-by-token streaming of the agent's output isn't wired yet (the turn returns when complete).
- Validate live — a real coding agent's behavior (and ACP version) is the true test; CI mocks it.
