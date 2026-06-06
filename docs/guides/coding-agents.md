# Spawn CLI coding agents (ACP)

An **optional, opt-in plugin** ([ADR 0024](/adr/0024-spawn-cli-coding-agents-acp))
that lets the lead agent hand a real coding job to a purpose-built **CLI coding
agent** — protoCLI (`proto`), Claude Code, Codex, Gemini CLI — and get the result
back.

Where `task()` delegates to an in-process LLM subagent and `peer_consult` talks
to a remote A2A peer, **`code_with(agent, task)`** spawns a coding agent that
carries its own file access, shell, repo-map, and edit/verify loop — so it can
read/edit/run code in a repo far better than a generic tool loop. It drives the
coding agent over the [Agent Client Protocol](https://agentclientprotocol.com)
(ACP): JSON-RPC 2.0 over the child's stdin/stdout. protoAgent is the ACP
*client*; `proto --acp` is the matching server.

> **Security:** a configured coding agent gets **file + shell access in its
> workdir** (auto-allowed, confined to that directory — see
> [Permission posture](#permission-posture)). The plugin therefore ships
> **disabled with no agents** — you enable it *and* declare agents explicitly.

## Enable it

The coding agent runs as a local subprocess, so this is configured in YAML, not
the in-app Settings (each agent grants local authority and deserves a deliberate
edit):

```yaml
# config/langgraph-config.yaml
plugins:
  enabled: [coding_agent]

coding_agent:
  default_timeout_s: 600          # coding is slow; per-agent override below
  agents:
    - name: proto                 # the name the LLM passes to code_with(agent=…)
      command: proto              # binary on PATH
      args: ["--acp"]             # ACP server mode
      workdir: ~/dev/my-repo      # session cwd — the confinement boundary
      # env: { SOME_KEY: value }  # optional extra env, merged over the process env
      # timeout_s: 900            # optional per-agent override (seconds)
      # permissions: allowlist    # auto (default) | allowlist | readonly
      # confirm: true             # ask the operator before each code_with call
```

Enabling plugins needs a **restart** (plugin tools wire once at process init).
On boot you'll see `[coding_agent] registered code_with for N agent(s)`.

### Other coding agents

Any agent that speaks ACP works — just point `command`/`args` at it:

```yaml
  agents:
    - name: proto
      command: proto
      args: ["--acp"]
      workdir: ~/dev/my-repo
    - name: claude-code
      command: npx
      args: ["@zed-industries/claude-code-acp"]
      workdir: ~/dev/my-repo
    - name: codex
      command: codex
      args: ["acp"]
      workdir: ~/dev/my-repo
    - name: gemini
      command: gemini
      args: ["--experimental-acp"]
      workdir: ~/dev/my-repo
```

The binary must be installed and on the `PATH` of the process running protoAgent.
A missing binary returns a clear error string to the agent (it doesn't crash).

## Use it

The lead agent calls the tool; the configured agent names appear in the tool's
description so the model knows what it can pass:

```
code_with(agent="proto", task="Add a GET /healthz route to server/, wire it
into the app, and run the tests. Report what you changed.")
```

Notes for whoever writes the `task`:

- The coding agent **does not see this conversation** — make `task` a
  self-contained brief: the goal, the relevant files if known, and the
  definition of done ("run the tests", "and lint").
- You **cannot** choose the directory — each agent works in its pre-configured
  `workdir`. To work in a different repo, configure another agent.
- The call **blocks** until the turn finishes (coding is slow). The default
  timeout is `default_timeout_s` (600s) unless the agent overrides it.
- **Follow-up calls to the same agent continue the same session** — so you can
  iterate: `code_with(agent="proto", task="now also add a test for it")`.

## Permission posture

A coding agent works in its **config-pinned workdir** (`code_with` takes only
`agent` + `task`, never a path — the model can't aim it elsewhere) and uses its
*own* file/shell access there: protoAgent advertises no client-served
`fs`/`terminal` capability. When the coding agent asks to do something risky it
sends a `session/request_permission`, which protoAgent answers with the agent's
**permission policy**:

| `permissions` | Behaviour |
|---|---|
| `auto` *(default)* | Allow everything — the agent self-governs within its workdir. |
| `allowlist` | Allow all action kinds **except** `execute` and `delete` (override with `allow_kinds` / `deny_kinds`). |
| `readonly` | Allow only read-like kinds (`read`, `search`, `fetch`, …); deny edits, shell, and deletes. |

Action kinds come from the ACP request (`toolCall.kind`: `read` / `edit` /
`execute` / `delete` / `fetch` / `move` / `search` / …). Tune a policy per agent:

```yaml
    - name: proto
      command: proto
      args: ["--acp"]
      workdir: ~/dev/my-repo
      permissions: allowlist
      deny_kinds: [execute, delete]   # the allowlist default, shown explicitly
```

### Per-call consent gate

Set `confirm: true` on an agent and `code_with` asks the operator to approve
**before each call** to that agent (via `ask_human` — the turn parks as
`input-required` until you reply `yes`):

```yaml
    - name: proto
      command: proto
      args: ["--acp"]
      workdir: ~/dev/my-repo
      confirm: true
```

> **Per-action** live HITL (approve each individual edit/shell command as the
> coding agent works) is **not** available: it would require pausing a blocking
> subprocess session mid-turn, which LangGraph's checkpoint/resume model can't do
> without re-running the tool. Use `permissions: readonly`/`allowlist` for
> deterministic per-action control, and `confirm` for a per-call human gate.

### Environment

The subprocess **inherits protoAgent's environment** (plus any per-agent `env`).
Run protoAgent under an account whose ambient credentials you're willing to lend
the coding agent, or scope the `workdir` to a throwaway checkout.

Coming in a later PR (ADR 0024): live narration of the coding agent's progress
("Editing app.py") onto A2A working-status frames.

## How it works

```
code_with(agent, task)
  → AcpClient (plugins/coding_agent/acp_client.py)
      → spawn `command args` in workdir, JSON-RPC 2.0 over its stdio:
        initialize → session/new(cwd) → session/prompt(task)
      ← session/update {agent_message_chunk}   → accumulated into the answer
      ← session/update {tool_call, title}        → narrated (logged)
      ← session/request_permission               → auto-allowed
  → returns the agent's final message text
```

One `AcpClient` (subprocess + session) is **cached per agent** so follow-up calls
continue the thread; a per-agent lock serializes turns (a session is a single
conversation — `task_batch` won't interleave two prompts on one).

See [Plugins](/guides/plugins) for the plugin model in general, and
[ADR 0024](/adr/0024-spawn-cli-coding-agents-acp) for the design rationale.
