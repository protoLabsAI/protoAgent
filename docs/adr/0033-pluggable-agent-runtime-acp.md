# ADR 0033 — Pluggable agent runtime (ACP executor)

**Status:** Accepted (shipped)

## Context

Today the agent's brain is fixed: a LangGraph `create_agent` loop driving a LangChain
chat model through the LiteLLM gateway (`graph/llm.py::create_llm`). The model provider is
swappable (Anthropic, OpenAI, vLLM, …) but the **runtime** — the loop that reasons + calls
tools — is always ours.

We want the option to power the runtime with an external **coding agent** —
**proto, GitHub Copilot CLI, OpenCode, Claude Code, Codex** — over the **Agent Client
Protocol (ACP)**, so an operator can run protoAgent with the agent (and subscription) they
already use. This *inverts* the existing ACP integration: ADR 0024's `code_with` has the
agent **call out** to a coding agent as a tool; here a coding agent **drives the turn**.

**Ecosystem (mature, 2026).** ACP is "the LSP for coding agents." Clients: Zed, JetBrains,
VS Code, Neovim, Emacs. Agents: Claude Code, Codex, Gemini CLI, Copilot CLI, OpenCode,
Goose, Cline, GLM, Kiro. There's an official ACP Agent Registry and an open headless client
lib (`openclaw/acpx`, adapters per agent). Anthropic's **Agent SDK** (2026-06-15) explicitly
licenses "third-party apps that authenticate with your Claude subscription," so "drive
protoAgent with your Claude sub" is sanctioned.

**Both comps already ship this and converge on one architecture** (researched OpenClaw +
Hermes). This ADR adopts that shape. It is **parity, not a unique edge** — our differentiation
is the operator *shell* we wrap around the brain (A2A-1.0-native, goals-with-verifiers,
scheduling, multi-instance, console, memory), not the ACP plumbing itself.

## Decision

### D1 — Runtime is a separate axis from the model

A new **`agent_runtime`** config axis selects *how* a turn executes, while the model
reference stays canonical (mirrors OpenClaw's `agentRuntime.id: "claude-cli"` +
`anthropic/claude-opus-4-8`):

- `agent_runtime: native` (default) — our LangGraph loop, unchanged.
- `agent_runtime: acp:<agent>` (e.g. `acp:proto`, `acp:codex`, `acp:claude`) — an external
  coding agent drives the turn over ACP.

Selection precedence (per-subagent override → per-call → config default) reuses the existing
model-resolution shape.

### D2 — Two runtime families behind one contract

- **Native (embedded):** the LangGraph loop. Owns the tool-call loop; tools/middleware as today.
- **ACP (external executor):** the coding agent runs *its own* loop with *its own* tools; we
  forward the turn and stream the result back to A2A/console. protoAgent stays the **shell**.

Both implement a small **runtime contract** (D4) so the rest of the system is runtime-agnostic.

### D3 — Tool plane: operator tools as one MCP bus

The coding agent reaches protoAgent's capabilities through ACP's own mechanism:
`session/new` accepts a client-supplied **`mcpServers`** list ("MCP gives the agent tools,
ACP gives it an editor"). So:

- protoAgent exposes its tools as a **single MCP server** (FastMCP / `to_fastmcp`), built from
  the live tool registry — **core + plugin `register_tools` tools uniformly** (plugins compose
  for free; no per-plugin MCP). This includes the meta-tools that trigger higher-order
  machinery: `task`/`task_batch` (subagents), `run_workflow`/`save_workflow` (workflows),
  `memory_recall`/`memory_ingest` (knowledge R/W), `set_goal`, `schedule_task`, notes/beads.
- Plugins that **are** an MCP server (`register_mcp_server`, e.g. Google) are **mounted
  directly** in `session/new`, not re-wrapped.
- Exposure is **allowlist-gated + opt-in** per runtime (don't hand `execute_code` etc. to an
  external brain). Default mirrors OpenClaw: nothing exposed unless configured.

So workflows / subagents / memory are *orchestrated* by the coding agent via tool calls and
*executed* by protoAgent internally — composes for free over MCP.

### D4 — Context plane: a runtime context contract (the keystone)

Things that are *injected* (not called) — SOUL/persona, retrieved knowledge, skills,
history/compaction — flow through a contract every runtime satisfies:

```
assemble_context(state) -> { stable_prefix, volatile_delta }
after_turn(result)      -> ingest (memory write-back) + compaction
```

- **Native runtime** satisfies it via existing middleware (KnowledgeMiddleware, skills, SOUL,
  compaction).
- **ACP runtime** satisfies it by building the prompt + session lifecycle.

This is the make-or-break, so it bakes in the caching discipline from D5.

### D5 — Caching discipline (non-negotiable)

Naively assembling everything every turn defeats cost *and* the coding agents' own prompt
caching. Rules, validated against both comps:

1. **ACP sessions are stateful — don't resend.** The agent owns history; send only the
   per-turn **delta**. Set context **once at `session/new`**.
2. **Immutable cacheable prefix.** `stable_prefix` (SOUL + static instructions + tool manifest)
   is byte-stable and prompt-cached (`cache_control`, native loop). **Never rebuild the prefix
   mid-session** — Hermes bug #13631: auto-injecting into the system prompt every N turns
   invalidates the KV cache on every prefix-caching backend. `volatile_delta` always goes
   *after* the prefix.
3. **Retrieval-on-demand > pre-injection.** Prefer the brain calling `memory_recall` /
   `search_skills` (tools on the bus) over bulk-stuffing — tiny, cache-stable payloads.
4. **Compaction: mirror & project, don't rewrite.** For ACP, lean on the agent's *own* session
   compaction; protoAgent's job is **after-turn durable write-back** (facts → knowledge store),
   echoing OpenClaw's "write durable facts before clearing" silent turn. Native loop keeps its
   structured rolling-summary compaction (à la Hermes Goal/Progress/Decisions/Files/Next-Steps).

### D6 — Reuse + scope

Generalize `plugins/coding_agent/acp_client.py` (ADR 0024) into a runtime-grade ACP client
(acpx-style per-agent adapters: proto/codex/claude/copilot/opencode), add `session/new`
`mcpServers` support, and wire it behind the `agent_runtime` axis. Permission gating reuses
the consent path from ADR 0024 (#599).

## Consequences

- **Parity, framed as our shell's superpower:** "bring your coding agent; we make it an
  operable, schedulable, goal-driven A2A service." This is not a unique edge — OpenClaw +
  Hermes both ship it; don't overclaim.
- **The operator-tools-as-MCP-server bridge (D3) is independently valuable** — any MCP client
  (Claude Desktop, Cursor) could then operate a protoAgent instance. Ship it first; the ACP
  runtime plugs into it.
- **The context contract (D4) is the real engineering** — tools are nearly free over MCP; the
  assemble/after-turn lifecycle + caching discipline is where quality lives.
- External brains are **isolated by default** (own tools only) until tools are allowlisted in —
  a deliberate trust boundary.
- ACP/coding-agent versions drift; the per-agent adapter layer absorbs that.

## Build order (proposed slices)

1. **Operator tools → MCP server** (FastMCP over the registry; allowlist; opt-in). Useful alone.
2. **Runtime context contract** — extract `assemble_context`/`after_turn`; native runtime
   implemented via current middleware (refactor, no behavior change).
3. **ACP runtime** — generalized acp_client + `agent_runtime: acp:<agent>` + `session/new`
   mcpServers + stream-to-A2A + permission gate.
4. **Goal/scheduler loops over ACP turns** + console runtime selector.

## References

- ADR 0024 (ACP coding-agent / `code_with`), ADR 0025 (delegate registry), ADR 0026 (plugin
  console surfaces), ADR 0032 (pluggable middleware).
- External: ACP (`agentclientprotocol.com` — session-setup `mcpServers`, `session/usage`),
  `openclaw/acpx`, OpenClaw agent-runtimes + context docs, Hermes `ContextEngine` +
  context-compression-and-caching, Anthropic Agent SDK (subscription-authed third-party apps).
