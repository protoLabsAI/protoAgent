# CLI coding agents over ACP

Hand a real coding job to a purpose-built **CLI coding agent** — protoCLI (`proto`),
Claude Code, Codex, Gemini CLI — and get the result back. A coding agent carries its
own file access, shell, repo-map, and edit/verify loop, so it reads/edits/runs code
in a repo far better than a generic tool loop.

You reach one through the unified [delegate registry](/guides/delegates) (ADR 0025)
as an **`acp` delegate**: `delegate_to(target, query)`. protoAgent is the ACP
*client*; `proto --acp` (or another CLI's ACP mode) is the matching server, driven
over the [Agent Client Protocol](https://agentclientprotocol.com) — JSON-RPC 2.0
over the child's stdin/stdout.

::: tip History
This used to be a standalone `coding_agent` plugin contributing a `code_with` tool
([ADR 0024](/adr/0024-spawn-cli-coding-agents-acp)). That tool was **retired** in
favour of `delegate_to` with an `acp` delegate, which does the same over one tool
alongside a2a/openai delegates and a console panel. The ACP client mechanics
described here are unchanged — `delegate_to` reuses them.
:::

> **Security:** a coding agent gets **file + shell access in its workdir** (confined
> to that directory — see [Permission posture](#permission-posture)). Declare it
> deliberately, and prefer a scoped/throwaway `workdir`.

## Configure an `acp` delegate

Coding agents run as local subprocesses, so they're declared in YAML (not in-app
Settings — each grants local authority and deserves a deliberate edit):

```yaml
# config/langgraph-config.yaml
delegates:
  - name: proto                 # the name you pass to delegate_to(target=…)
    type: acp
    description: Coding agent — implements a change in a repo.
    command: proto              # binary on PATH
    args: ["--acp"]             # ACP server mode
    workdir: ~/dev/my-repo      # session cwd — the confinement boundary
    permissions: allowlist      # auto | allowlist | readonly
    # env: { SOME_KEY: value }  # optional extra env, merged over the process env
    # timeout_s: 900            # optional per-call timeout (seconds)
    # allow_kinds: []           # override: kinds to allow
    # deny_kinds: [execute, delete]   # override: kinds to deny
```

The delegates registry is **enabled by default** — there's no plugin to turn on.
Declaring (or editing) the `delegates` list hot-reloads on **Save & Reload**: the
first delegate you add registers `delegate_to` for the next turn, no restart.

### Other coding agents

Any agent that speaks ACP works — just point `command`/`args` at it:

```yaml
delegates:
  - { name: proto,       type: acp, command: proto,       args: ["--acp"],              workdir: ~/dev/my-repo }
  - { name: claude-code, type: acp, command: claude-code, args: [],                     workdir: ~/dev/my-repo }   # alias → claude-agent-acp
  - { name: codex,       type: acp, command: codex-acp,   args: [],                     workdir: ~/dev/my-repo }   # @zed-industries/codex-acp adapter (codex has no native ACP)
  - { name: opencode,    type: acp, command: opencode,    args: ["acp"],                workdir: ~/dev/my-repo }
  - { name: copilot,     type: acp, command: copilot,     args: ["--acp"],              workdir: ~/dev/my-repo }
  - { name: gemini,      type: acp, command: gemini,      args: ["--experimental-acp"], workdir: ~/dev/my-repo }
```

The binary must be installed and on the `PATH` of the process running protoAgent.
The delegates panel's **Test** button performs a real ACP `initialize` handshake — so
a wrong launch command **fails the probe** instead of showing green (it's not just a
missing-binary check). A misconfigured delegate surfaces at Test, not at first dispatch.
The probe resolves the command against the **same** PATH the spawn uses — the process
PATH with the delegate's `env` PATH overlaid — so probe and dispatch never disagree.

::: warning macOS desktop app & `PATH`
A GUI app launched from Finder/Dock/`launchd` inherits only `launchd`'s minimal `PATH`
(`/usr/bin:/bin:/usr/sbin:/sbin`), **not** your login-shell `PATH` — so Homebrew
(`/opt/homebrew/bin`), nvm, Volta, and asdf installs (where `npx`/`node`/ACP adapters
live) are invisible, and a `command: npx` delegate fails with `binary not on PATH`
([#1299](https://github.com/protoLabsAI/protoAgent/issues/1299)). The desktop build now
hands the bundled server your real login-shell `PATH`, so this works out of the box.
If you still hit it (an unusual shell setup), either set an **absolute** `command`
(`/opt/homebrew/bin/npx`) or add a `PATH` to the delegate **`env`** — both pass the
probe too. The web app (terminal-launched server) is unaffected.
:::

**Claude Code has no native ACP mode.** Drive it through the
[`claude-agent-acp`](https://www.npmjs.com/package/@agentclientprotocol/claude-agent-acp)
adapter: install it (`npm i -g @agentclientprotocol/claude-agent-acp`) and use the
`claude-code` alias above — it maps to `command: claude-agent-acp` with no args, so you
don't have to know the incantation. (The older `@zed-industries/claude-code-acp` is
**deprecated** — it was renamed to `@agentclientprotocol/claude-agent-acp`.) Setting
`command: claude` directly does **not** work — `claude` isn't an ACP server, and the
probe will tell you so.

> **Nested Claude:** the adapter launches the `claude` binary, which **refuses to
> start nested inside another Claude Code session** (`Error: Claude Code cannot be
> launched inside another Claude Code session`). protoAgent now **strips the
> nested-session markers** (`CLAUDECODE` and the whole `CLAUDE_CODE_*` family) from
> the ACP launch env automatically ([#1296](https://github.com/protoLabsAI/protoAgent/issues/1296)),
> so launching protoAgent from *within* a `claude` session — the dogfooding case —
> works without the manual `env -u …` dance. (Partial strips were the footgun: missing
> just one of `CLAUDE_CODE_SESSION_ID` / `CLAUDE_CODE_ENTRYPOINT` / … still tripped the
> guard, so the agent respawned every ~2 min with no surfaced error.) A value you set
> explicitly in the delegate `env` still wins.

**Codex has no native ACP mode either.** Recent `codex` CLI (≥ 0.13x) dropped the
`acp` subcommand — it speaks **MCP** natively, not ACP, so `command: codex, args:
["acp"]` no longer works (the probe fails). Drive it through the
[`@zed-industries/codex-acp`](https://www.npmjs.com/package/@zed-industries/codex-acp)
adapter: install it (`npm i -g @zed-industries/codex-acp`) → `command: codex-acp`, or
run it zero-install with `command: npx, args: ["-y", "@zed-industries/codex-acp"]` (the
form the [ACP-runtime](/guides/acp-runtime) and [MCP](/guides/mcp) guides use).

**opencode** (`opencode acp`) and **GitHub Copilot CLI** (`copilot --acp`) ship native
ACP servers — point `command`/`args` straight at them, no adapter needed.

## Use it

The lead agent calls `delegate_to`; configured delegates appear in the tool's
description:

```
delegate_to(target="proto", query="Add a GET /healthz route to server/, wire it
into the app, and run the tests. Report what you changed.")
```

Notes for whoever writes the `query`:

- The coding agent **does not see this conversation** — make `query` a
  self-contained brief: the goal, the relevant files if known, and the definition
  of done ("run the tests", "and lint").
- The delegate works in its configured `workdir`. To target a different tree,
  declare another delegate — or, programmatically, dispatch a `workdir`-scoped copy
  (the board loop does this per feature; see below).
- The call **blocks** until the turn finishes (coding is slow), up to `timeout_s`.
- **Follow-up calls reuse the cached session** — so you can iterate
  (`delegate_to("proto", "now also add a test for it")`).

## Permission posture

A coding agent works in its **configured workdir** and uses its *own* file/shell
access there; protoAgent advertises no client-served `fs`/`terminal` capability.
When the coding agent asks to do something risky it sends a
`session/request_permission`, which protoAgent answers with the delegate's
**permission policy**:

| `permissions` | Behaviour |
|---|---|
| `auto` *(default)* | Allow everything — the agent self-governs within its workdir. |
| `allowlist` | Allow all action kinds **except** `execute` and `delete` (override with `allow_kinds` / `deny_kinds`). |
| `readonly` | Allow only read-like kinds (`read`, `search`, `fetch`, …); deny edits, shell, and deletes. |

Action kinds come from the ACP request (`toolCall.kind`: `read` / `edit` /
`execute` / `delete` / `fetch` / `move` / `search` / …).

> **Per-action** live HITL (approve each individual edit/shell command as the agent
> works) is **not** available — it would require pausing a blocking subprocess
> session mid-turn. Use `permissions: readonly`/`allowlist` for deterministic
> per-action control. With no container isolation, the `workdir` is the sandbox:
> scope it to a throwaway checkout (or a disposable git worktree) for untrusted runs.

### Environment

The subprocess **inherits protoAgent's environment** (plus any per-delegate `env`),
**minus** the nested-Claude markers (`CLAUDECODE` / `CLAUDE_CODE_*`) — see the caveat
above. Run protoAgent under an account whose ambient credentials you're willing to lend
the coding agent, or scope the `workdir` to a throwaway checkout.

## How it works

```
delegate_to(target="proto", query=…)
  → AcpAdapter.dispatch (plugins/delegates/adapters.py)
      → AcpClient (plugins/coding_agent/acp_client.py)
          → spawn `command args` in workdir, JSON-RPC 2.0 over its stdio:
            initialize → session/load(saved id) or session/new(cwd) → session/prompt(query)
          ← session/update {agent_message_chunk}  → accumulated into the answer
          ← session/update {agent_thought_chunk}  → surfaced as the reasoning trace
          ← session/update {tool_call, title}       → narrated (logged)
          ← session/request_permission              → answered by the policy
  → returns the agent's final message text
            … session/cancel on abort · session/close on teardown
```

One `AcpClient` (subprocess + session) is **cached per launch+policy signature**
(the key includes `workdir`) so follow-up calls reuse the session. A caller that
dispatches into a **transient, per-call `workdir`** — e.g. `dataclasses.replace`ing
a delegate onto a disposable git worktree — should call `AcpAdapter.teardown(d)` in
a `finally` to reap that worktree's subprocess (a plain cache drop forgets the handle
but leaves the process alive).

### Sessions survive a restart

The `sessionId` is persisted per launch signature (under `~/.protoagent/acp_sessions/`).
On the next start, if the agent advertises the ACP `loadSession` capability the client
**`session/load`s the saved thread** (replaying its history silently to reattach)
instead of starting fresh — so a crash, a CI bounce, or a re-dispatch continues the
same coding thread rather than losing its context. A stale or unknown id falls back to
a fresh `session/new`. The ACP `protocolVersion` is negotiated at `initialize`; the
client closes the connection if the agent counters with a version it doesn't speak.

## Eval it

A gated eval case (`acp_delegation`) verifies end-to-end delegation against a live
agent. It's skipped unless you opt in — configure an `acp` delegate, then:

```bash
export EVAL_CODING_AGENT=1
python -m evals.runner --tasks acp_delegation
```

It drives a real A2A turn that asks the agent to use `delegate_to`, and asserts (via
the audit channel) that the tool fired. Without `EVAL_CODING_AGENT` set it `SKIP`s,
so it never breaks the default board. See [Eval your fork](/guides/evals).

See [Delegates](/guides/delegates) for the registry + panel, [Plugins](/guides/plugins)
for the plugin model, and [ADR 0024](/adr/0024-spawn-cli-coding-agents-acp) /
[ADR 0025](/adr/0025-unified-delegate-registry-and-panel) for the design rationale.
