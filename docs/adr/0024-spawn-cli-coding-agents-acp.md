# 0024 — Spawn CLI coding agents over ACP (`code_with`)

Status: **Accepted** (PR1 — thin vertical)

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

### Confinement & permission posture (PR1)

- **Workdir is config-pinned.** `code_with` takes only `agent` + `task` — never a
  caller-chosen path. The cwd comes from the matched config entry, so the LLM
  cannot point a coding agent at an arbitrary directory. Workdirs must be listed
  in config; an unknown `agent` returns an error listing the configured ones.
- **Auto-allow within the workdir.** The client advertises no client-served
  `fs`/`terminal` capability, so the coding agent uses its *own* file/shell
  access, scoped to the session cwd. Inbound `session/request_permission` is
  auto-approved (first `allow` option), mirroring ORBIS's PR1 policy. The coding
  agent self-governs inside its sandbox dir.
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

**PR1 (this ADR):** ACP client + `code_with` + config + auto-allow + tests +
docs. Synchronous — the final answer is returned; `tool_call` titles are logged,
not yet streamed to callers.

**Later PRs:** live narration of `tool_call` titles onto A2A working-status
frames (so an operator watching a turn sees "Editing app.py"); richer permission
policy (HITL gating via `ask_human`, allow-by-kind); more shipped agent recipes
(claude-code-acp, codex, gemini); an eval case.

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
