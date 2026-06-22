# 0024 — Spawn CLI coding agents over ACP (`code_with`)

Status: **Superseded by [ADR 0025](./0025-unified-delegate-registry-and-panel.md)** — the
`code_with` tool was retired in favour of `delegate_to` with an `acp` delegate, which
reuses the ACP client mechanics below. `plugins/coding_agent/` remains as the shared ACP
client library. (Originally: Accepted, PR1 — thin vertical.)

## Update (2026-06) — full session lifecycle adopted

PR1 was a deliberate "thin vertical" (handshake → one turn → auto-allow), so the
mechanics described below are no longer the whole story. The shared client
(`plugins/coding_agent/acp_client.py`) has since grown the full ACP session
lifecycle:

- **#969** — best-effort `session/cancel` on prompt abort (timeout / external
  cancel) and actionable `AUTH_REQUIRED` handling (the client no longer surfaces an
  opaque error when the agent isn't authenticated).
- **#972** — `session/load` reattach for **restart-surviving threads** (gated on the
  agent advertising the `loadSession` capability; the `sessionId` is persisted per
  launch signature), `session/close` on teardown, real `protocolVersion`
  negotiation (closes if the agent counters with an unsupported version), and
  `agent_thought_chunk` surfaced as the reasoning trace.
- **#1296 / #1300** — launch + probe hardening. The ACP launch env now **strips the
  nested-Claude markers** (`CLAUDECODE` / `CLAUDE_CODE_*`) so a `claude-agent-acp`
  backend doesn't hit the "inside another Claude Code session" guard when protoAgent
  itself runs inside a Claude session (the dogfooding case); the round-trip
  (`initialize` → session → `session/prompt` → `request_permission`) is now logged so
  an idle freeze is diagnosable. A new `AcpClient.handshake()` runs **`initialize`
  only** (no `session/new`/`session/load`), and the delegate health prober uses it —
  so a status probe is genuinely cheap + side-effect-free instead of opening a real
  session every 120s.

Validated end-to-end against both `proto --acp` and Claude Code (via
`@zed-industries/claude-code-acp`) — the client is agent-agnostic. See the
[coding-agents guide](/guides/coding-agents) for the current behaviour and config.

## Context

protoAgent's lead agent already delegates in two ways: to **in-process LLM
subagents** via `task()` (DeerFlow pattern, `graph/subagents/`) and to **remote
A2A peers** via `peer_consult` (`tools/peer_tools.py`). Both are *talk to another
model* delegations — neither can pick up a repo, edit files, run the test suite,
and hand back a diff.

There is a whole class of work that wants exactly that: "add a `/healthz` route
and run the tests", "fix the failing import in `server/chat.py`". A purpose-built
**CLI coding agent** — protoCLI (`proto`), Claude Code, Codex, Gemini CLI — does
this far better than a generic tool loop, because it carries its own file access,
shell, repo-map, edit/verify harness, and approval UX.

The protoLabs companion stack already solved this. **ORBIS** (the Python voice
companion) is an **ACP client**: it launches a coding agent as a subprocess and
drives one session over the [Agent Client Protocol](https://agentclientprotocol.com)
— JSON-RPC 2.0, newline-delimited, on the child's stdin/stdout. The same
`delegate_to` registry routes `a2a` / `openai` / `acp` delegate types. protoCLI,
on the other side, speaks the matching server role (`proto --acp`).

This ADR brings the **ACP-client leg** into protoAgent so our agents can spawn
CLI coding agents. We do **not** port ORBIS's whole `delegate_to` registry —
protoAgent already covers the `a2a` and `openai` legs differently (peers +
subagents + the LiteLLM gateway). The new capability is just the ACP one.

## Decision

Ship ACP-client support as a **first-party, opt-in plugin** (`plugins/coding_agent`),
not a core tool. It contributes one tool — `code_with(agent, task)` — backed by
a small ACP client (a port of ORBIS's `acp/client.py`).

### Why a plugin, not core

- The plugin seam already gives config + secrets + Settings + enable/disable for
  free, with **zero core edits** — matching the operator-fork contract (ADR 0019)
  and the Discord/Google precedent (ADR 0018).
- Spawning a coding agent with file + shell access in a workdir is a real
  authority delegation. It should be **off by default** and explicitly opted into,
  exactly like the shipped `hello` example plugin (`enabled: false`).
- Not every fork wants this. A plugin keeps the default tool surface lean
  (ADR 0005, tool pollution).

### Shape

```
plugins/coding_agent/
  protoagent.plugin.yaml   # config_section: coding_agent; agents: []; enabled: false
  acp_client.py            # AcpClient — JSON-RPC 2.0 over the child's stdio
  __init__.py              # register(): builds code_with from configured agents
```

Config (a top-level `coding_agent` section, ADR 0019):

```yaml
coding_agent:
  default_timeout_s: 600        # coding is slow; per-agent override available
  agents:
    - name: proto              # the name the LLM passes to code_with(agent=…)
      command: proto           # binary on PATH
      args: ["--acp"]          # ACP server mode
      workdir: ~/dev/my-repo   # session cwd — the confinement boundary
      # env: { FOO: bar }      # optional extra env (merged over the process env)
      # timeout_s: 900         # optional per-agent override
```

The tool the lead agent sees:

```
code_with(agent="proto", task="add a /healthz route and run the tests")
  → the agent's final message text (the work happens in its own session)
```

### Confinement & permission posture

- **Workdir is config-pinned.** `code_with` takes only `agent` + `task` — never a
  caller-chosen path. The cwd comes from the matched config entry, so the LLM
  cannot point a coding agent at an arbitrary directory. Workdirs must be listed
  in config; an unknown `agent` returns an error listing the configured ones.
- **By-kind permission policy (PR3).** The client advertises no client-served
  `fs`/`terminal` capability, so the coding agent uses its *own* file/shell
  access, scoped to the session cwd. Inbound `session/request_permission` is
  answered by the agent's `permissions` policy, keyed on the request's
  `toolCall.kind`: `auto` (allow all — the PR1 default, mirroring ORBIS), `allowlist`
  (deny `execute`/`delete`, allow the rest), or `readonly` (allow only read-like
  kinds). Overridable per agent with `allow_kinds` / `deny_kinds`.
- **Per-call consent gate (PR3).** `confirm: true` makes `code_with` ask the
  operator (via `ask_human` → `input-required`) to approve *before each call* to
  that agent. The gate runs before any side effect, so the LangGraph resume
  re-execution is idempotent.
- **Per-action live HITL is deferred** — approving each individual edit/shell
  command mid-turn would require pausing a *blocking subprocess session* while
  asking the operator. LangGraph's `interrupt()` checkpoints and **re-runs the
  node** on resume, which can't resume a half-finished ACP session awaiting a
  specific permission response. `readonly`/`allowlist` give deterministic
  per-action control; `confirm` gives a per-call human gate. (Same coupling blocks
  live narration — see Scope.)
- The subprocess inherits the server's env (plus any per-agent `env`). Run
  protoAgent under an account whose ambient credentials you're willing to lend
  the coding agent — or scope its `workdir` to a throwaway checkout.

### Wire protocol (ACP, client side)

```
→ initialize        {protocolVersion: 1, clientCapabilities: {fs:{…false}, terminal:false}}
→ session/new       {cwd, mcpServers: []}                       ← {sessionId}
→ session/prompt    {sessionId, prompt: [{type:text, text}]}    ← {stopReason}
← session/update    {update:{sessionUpdate:"agent_message_chunk", content:{text}}}   (accumulated → answer)
← session/update    {update:{sessionUpdate:"tool_call", title}}                       (narration → logged)
← session/request_permission  {options:[…]}  → auto-allow
```

One `AcpClient` owns one subprocess + one session, **cached per agent** so
follow-up `code_with` calls continue the same thread (mirrors the A2A peer's
sticky `contextId`). A per-agent lock serializes turns (a session is a single
conversation; `task_batch` must not interleave two prompts on one session).

## Scope

**PR1 (#596):** ACP client + `code_with` + config + auto-allow + tests + docs.
Synchronous — the final answer is returned; `tool_call` titles are logged.

**PR3 (this update):** by-kind permission policy (`auto`/`allowlist`/`readonly` +
`allow_kinds`/`deny_kinds`); per-call consent gate (`confirm`); shipped agent
recipes (claude-code-acp, codex, gemini). Per-action live HITL is documented as
deferred (the blocking-session constraint above).

**PR4:** a gated eval case (`code_with_delegation`) verifying end-to-end
delegation over a live A2A turn, plus a `requires_env` skip mechanism in the eval
runner so the case (and any future case needing an optional integration) is
skipped — not failed — when its prerequisite env var is unset.

**Later PRs:** live narration of `tool_call` titles onto A2A working-status
frames (so an operator watching a turn sees "Editing app.py") — blocked on the
same mid-session channel as per-action HITL, so it wants a general progress
mechanism (event-bus ride-along or a stream-factory `progress` event), not a
one-off.

## Consequences

- protoAgent gains a third delegation altitude: **hand a real coding job to a
  purpose-built CLI agent** and get the result back — without forking.
- The security surface is explicit and opt-in: disabled by default, empty agent
  list by default, workdir-confined, documented.
- We take a dependency on the target agent being installed and on PATH; a missing
  binary returns a clear error string, not a crash.

## Alternatives considered

- **Core tool module** (`tools/acp_tools.py`) — always present like `peer_tools`.
  Rejected: edits core for a capability most forks won't use, and loses the
  free config/Settings/enable-disable plumbing.
- **Full `delegate_to` registry port** (a2a + openai + acp) — most faithful to
  ORBIS. Rejected for now: largest blast radius, and it overlaps protoAgent's
  existing peer + subagent + gateway seams. The ACP leg is the only genuinely
  missing one.
