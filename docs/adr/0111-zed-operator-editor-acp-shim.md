# 0111 — Zed as the operator's editor: ACP agent shim, editor deep-links, operator MCP

- Status: Proposed (spike: `integrations/zed-acp/`, PR on `spike/zed-acp-shim`)
- Date: 2026-09-23
- Amends: [ADR 0075](./0075-external-interfaces-cli-mcp-api.md): its framing that the
  **ACP-server** role belongs to protoCLI's `proto --acp` (lines ~65/77/140). That still
  holds for *proto's* agent. It does not cover *protoAgent's own* agent, which this ADR
  exposes over ACP (D1). An amendment note is added to 0075 in place.
- Builds on: [ADR 0033](./0033-pluggable-agent-runtime-acp.md) (protoAgent as an ACP
  *client*; it lists Zed as an ACP client, and the operator MCP server comes from there),
  [ADR 0024](./0024-spawn-cli-coding-agents-acp.md)/[0025](./0025-unified-delegate-registry-and-panel.md)
  (ACP delegation), [ADR 0042](./0042-fleet-supervisor-unified-console.md) (fleet members
  reached over A2A), [ADR 0095](./0095-managed-projects-registry.md) (project roots),
  [ADR 0104](./0104-session-turns-read-api.md) (`chat-` sessions)

## Context

Operators read and steer protoAgent's work in code. Today they do that from the console,
the fleet deck, or a terminal next to an editor. Zed is the editor several of us use, and
its **Agent Panel** hosts *external agents* over the [Agent Client Protocol](https://agentclientprotocol.com)
(ACP): JSON-RPC 2.0 on stdio, protocol version 1. The client sends `initialize`,
`session/new`, `session/prompt` and `session/cancel`. The agent answers with
`session/update` notifications: message and thought chunks, plus `tool_call` /
`tool_call_update`. A tool call may carry `locations: [{path, line?}]`, with absolute
paths and 1-based lines. Zed's **follow the agent** mode jumps the editor to those
locations as the agent works.

Facts that constrain the design:

1. **Zed launches external agents only as local stdio subprocesses.** You configure them
   under `agent_servers` in settings (`{"type": "custom", "command", "args", "env"}`) or
   install them from the **ACP Registry**. There is no "connect to a URL" option. The ACP
   *Streamable HTTP & WebSocket transport* RFD was merged in April 2026 and moved to
   *Active* on 2026-07-02. Its reference implementation is in Goose. The Python SDK ships
   an experimental `acp.http`, but Zed does not connect to a remote ACP agent over HTTP.
2. **protoAgent is an ACP client only.** `plugins/coding_agent/acp_client.py` and
   `runtime/acp_*` have no ACP server. What protoAgent *does* serve is A2A 1.0 JSON-RPC on
   `/a2a`, with SSE streaming. That wire is the one the console, the deck and every fleet
   peer already speak.
3. **Zed's extension route is closed for this.** Zed has deprecated agent servers provided
   by extensions: "The ACP Registry is now the way to install agents, and previously
   installed extension agents are automatically migrated." Zed extensions (WASM) cannot add
   UI panels. Extension slash commands belonged to the old text-thread assistant and do not
   reach an external agent's thread.
4. **Registry listing needs authentication.** The registry CI requires that the
   `initialize` response lists `authMethods` with at least one method of type `agent` or
   `terminal`. Distribution options are a binary archive, `npx`, or `uvx` (a PyPI
   package).

## Decision

### D1: `protoagent-acp`, a standalone stdio ACP agent that is a pure A2A client

The new package lives in `integrations/zed-acp/`. It has its own `pyproject.toml`, depends
only on `agent-client-protocol` 0.12.x and `httpx`, and runs as
`uvx --from <path | git-subdir | PyPI> protoagent-acp --url … [--token-file …]`.
It talks to a **running** instance over A2A, so the same binary works against:

- the local instance;
- a desktop fleet member, directly by port, or through the hub's `/agents/<slug>` proxy
  with `--slug`;
- a remote box.

It is **outside the core import graph and outside the wheel.** `lint-imports` only covers
`root_packages`, and ruff lints the new files like any other. The package cannot import
`deck/` or the core. It restates the few A2A wire facts it needs (proto method names,
`A2A-Version: 1.0`, tool-call-v1 on status metadata, append-only-on-explicit-`true`) and
cites where each one comes from.

| ACP | A2A / protoAgent |
|---|---|
| `initialize` | Reports capabilities (`embeddedContext`; no `loadSession`) and `authMethods` |
| `authenticate` | Reloads the credential, then probes with `GetTask` on an id that cannot exist |
| `session/new` | Creates a fresh `contextId` `chat-zed-<ms>-<rand>`. The ACP sessionId *is* the A2A contextId, so the thread also shows in the console chat list (ADR 0104) |
| `session/prompt` | `SendStreamingMessage`. The first prompt gets a one-line preamble when Zed's `cwd` is one of the agent's projects |
| `session/cancel` | `CancelTask` on the streaming task. The prompt returns `stopReason: cancelled` |
| `agent_message_chunk` | Artifact text on `append: true`. The terminal REPLACE is de-duplicated: ACP can't retract, so only an unseen suffix is emitted |
| `agent_thought_chunk` | reasoning-v1 DataPart |
| `tool_call` → `tool_call_update` | tool-call-v1 `started` (announced twice: first with empty args, then with args) → `completed`/`failed` |
| `kind` | `read_file`/`list_dir` → read · `search_files`/`find_files` → search · `write_file`/`edit_file` → edit · `delete_file` → delete · `run_command` → execute · `task` → think · otherwise other |
| `locations` | `project` + relative `path` (+ `offset` as line) resolved against the project roots. `search_files` hits (`rel:line:`) become locations on completion. A path that escapes its root is never emitted |
| `session/request_permission` | A hitl-v1 `approval` park. The answer resumes the same task with `approved`/`denied` in the same prompt, and a failure to ask fails closed |
| a question / form park | Shown as text and the turn ends. The **next prompt** resumes the task (`metadata.hitl_resume`), the same way the console does it |
| `PromptResponse.usage` | cost-v1 on the terminal artifact |
| **a failed turn** | protoAgent emits `SUBMITTED → WORKING → FAILED`, with the exception text as the FAILED status message's only part. For example, `Error code: 429 - {… 'usage_limit_reached' …}` when the model's quota is spent; `server/chat.py` yields `("error", str(e))` and the executor calls `updater.failed(...)`. The shim sends a `⚠️ protoAgent error: <friendly message>` chunk, which stays in the thread history, and returns a **JSON-RPC error** from `session/prompt`. Zed renders that as its error callout (`ThreadError::Other`). A plain `end_turn` would read as an empty success |
| a stream that ends with no terminal state | The durable task is read back with `GetTask`. `completed` emits any answer text not yet shown. `failed` is handled as above. Still `working` is an error ("may finish on its own; check the console"), and so is an unreadable task. It never ends as a silent `end_turn` |

**Project roots** are resolved in this order:

1. `--root name=/local/path` overrides. These are required for a remote instance, whose
   paths live on another machine.
2. `GET /api/fs/roots` (a parallel core PR; a 404 on older instances is normal).
3. `GET /api/config` `filesystem.projects` (the explicit fence, which *shadows* the
   registry) together with `GET /api/projects` (the ADR 0095 registry). `/api/projects`
   alone misses the explicit roots. On protoEngineer it reports `fence_source: explicit`
   and omits `protoAgent → ~/dev/protoAgent-team`.
4. Roots learned from a `list_projects` result that went past in the stream.

When a tool names a project that has no root, the map is **reloaded**. That covers an
agent that `onboard_project`-ed a repo mid-session, which the navaEngineer rehearsal hit.
Reloads are rate-limited per name, so repeated calls on an unmappable project cost one
fetch.

**Credentials** are taken from `--token` / `--token-file`, then `PROTOAGENT_TOKEN` /
`PROTOAGENT_TOKEN_FILE`, then the file written by `protoagent-acp login`. That file is
mode 0600 and bound to its URL, so a stored token is never sent to a different instance.
`authMethods` advertises `terminal` (`args: ["login"]`), which satisfies the registry, and
`env_var` (`PROTOAGENT_TOKEN`). stdout carries protocol only. Logs go to stderr, and the
httpx/httpcore loggers are pinned to WARNING.

### D2: Evidence: live against protoEngineer (v0.172.0, unmodified)

A test harness (`scripts/acp_harness.py`) starts the shim over stdio the way Zed does,
using the SDK's `spawn_agent_process`. It then runs `initialize → session/new →
session/prompt` against the live `protoEngineer-ba4c` on :7875 with its fleet token. The
prompt was a read-only question. The full stream is in
`integrations/zed-acp/examples/protoengineer-transcript.txt`:

```
  5.7s tool_call        toolu_01WRAGmu kind='search' status='in_progress'
  7.6s tool_call_update toolu_01WRAGmu title="Search protoAgent for 'tool_start'"
  8.2s tool_call_update toolu_01WRAGmu status='completed' locations=[{"path": "/Users/kj/dev/protoAgent-team/CHANGELOG.md", "line": 4301}, …]
 12.2s tool_call        toolu_01YGciFn kind='read' status='in_progress' title='Read file'
 14.2s tool_call_update toolu_01YGciFn title='Read protoAgent/a2a_impl/executor.py (lines 785–839)'
                        locations=[{"path": "/Users/kj/dev/protoAgent-team/a2a_impl/executor.py", "line": 785}]
 16.8s text  "Here's exactly where the A2A executor turns …"
<<< stopReason: end_turn usage: {'totalTokens': 191245, 'inputTokens': 189945, 'outputTokens': 1300, 'cachedReadTokens': 128401}
```

Streamed text, reasoning, tool kinds and **absolute `locations` into the agent's checkout**
all work with **no core change**. The A2A stream already carries the tool name and args.
Zed-side behavior was not exercised by a human in this spike; the harness is the stand-in.

### D3: What the A2A stream is missing (core follow-ups, not this spike)

- **Args are an 800-char preview string, not structured data.**
  `server/chat.py::_coerce_tool_value` (`_TOOL_PREVIEW_CHARS = 800`) JSON-encodes a call's
  args and truncates the result. For fs tools the scalars (`project`, `path`, `offset`) come
  first, so the shim recovers them from truncated JSON with a regex. It cannot recover
  `write_file.content` or `edit_file.old/new`, so it cannot render an ACP `diff` for an
  edit. The fix is in `a2a_impl/executor.py::_tool_call_frame`: add a structured
  `locations` hint (`[{project, path, line}]`) and, for edit tools, an untruncated
  `{path, old, new}` in the tool-call-v1 fragment. Both extra-key lanes already exist
  (`parentToolCallId`, `outputChars`). Compute them in `server/chat.py` at
  `on_chat_model_end`, where the full `tc["args"]` is still in hand.
- **Results are capped at 800 chars too**, so search-hit locations cover only the first
  few hits.
- **Project roots have no stable endpoint on older instances** (`/api/fs/roots` is in
  flight). Until it lands, the shim uses the `/api/config` + `/api/projects` union.

### D4: File writes: protoAgent keeps writing its own roots (option a). Route through the editor (b) later, opt-in, via a park

**(a) Report only.** protoAgent's `write_file`/`edit_file` write the project root on the
instance's machine. The shim shows an `edit` card with a location, and Zed follows the
file. Zed reloads the buffer from disk when the instance is on the same machine.
Upside: nothing about the agent changes, fences and approvals stay protoAgent's, and it
works for remote instances. Downside: Zed's **Review Changes** and per-hunk accept/reject
only track edits made through ACP `fs/write_text_file`, so they don't light up.

**(b) Editor-routed writes.** The shim would need protoAgent to *hand a write back to the
client* instead of performing it. A2A has no client-executed-tool primitive, but
protoAgent has a working one-shot equivalent in the HITL park (`input-required` +
hitl-v1 → resume). A session flag (`metadata.editor_fs: true`) would make fs write tools
park with `{"kind": "fs_write", "path", "content"}`. The shim would call
`fs/write_text_file`, optionally after `request_permission`, then resume with `written` or
an error. Costs:

- one park/resume round trip per write;
- the tool's fence must still validate the path server-side before parking;
- paths must be the *editor's* filesystem, which is only true for a same-machine instance
  or through `--root` remapping;
- the agent's view of the file diverges until the resume.

**Recommendation: ship (a).** Build (b) only when an operator actually wants per-hunk
review of agent edits in Zed, and make it per session. It rides the existing HITL seam, so
the core change is contained: fs tools plus one hitl kind. The spike does not build it.

### D5: The other two surfaces are recorded here, not built

1. **Editor deep-links.** Zed registers the `zed://` scheme. `zed://file/<abs path>:<line>:<col>`
   opens a file at a position (`crates/zed/src/zed/open_listener.rs`: `parse_file_path` →
   `PathWithPosition`). `zed://agent?prompt=<urlencoded>` opens the Agent Panel with a
   prompt filled in. The console and deck can offer "Open in Zed" on tool cards and file
   references, behind an operator setting for the editor. This is pure presentation, needs
   no protocol work, and is the cheapest win.
2. **Operator MCP as a Zed context server.** `protoagent operator-mcp`
   (`server/operator_mcp.py`, ADR 0033) serves the allowlisted operator tools over MCP stdio.
   Zed's `context_servers` can mount it for **Zed's own agent**, so Zed's model can read the
   board and notes, set goals and schedule work on a protoAgent. Caveat: it builds the
   tool registry **in-process from the instance config**. It is not a client of a running
   instance, so it is local-only (`PROTOAGENT_HOME` must point at the instance), and
   `operator_mcp.tools` is empty (so nothing is exposed) until the operator allowlists
   tools. The shim does **not** forward the `mcpServers` Zed offers in `session/new`: a
   server Zed runs locally is unreachable from a remote instance, and mounting servers per
   session is not an A2A concept.

### D6: Distribution and registry path

- Now: `uvx --from git+https://github.com/protoLabsAI/protoAgent@<ref>#subdirectory=integrations/zed-acp protoagent-acp`,
  or a local path.
- Registry: publish `protoagent-acp` to PyPI through the plugin release ritual (manifest,
  pyproject and lock; never `gh release create`). Then submit `agent.json` with
  `"distribution": {"uvx": {"package": "protoagent-acp@<ver>"}}`, a license URL, and a
  16×16 `icon.svg`. The `terminal` auth method satisfies the registry CI. A registry
  install defaults to `http://127.0.0.1:7870`, and `login` stores a different URL.

## Alternatives considered

- **A Zed WASM extension.** Agent-server extensions are deprecated in favor of the registry
  and are auto-migrated. Extensions can't add UI, and slash-command extensions don't reach
  external-agent threads. Rejected.
- **An ACP server inside core (`protoagent acp`).** Zed spawns the agent locally, so the
  process has to run where the editor is. Bundling the whole runtime to relay a stream it
  could fetch over A2A is the wrong trade, and a remote fleet member would still need a
  network hop. A core CLI alias that execs the shim is fine later.
- **Serve ACP-over-HTTP from the instance (`/acp`).** This is the right end state once Zed
  speaks the HTTP transport, and it would supersede the shim's network half. Not possible
  today (Context §1).
- **Use `proto --acp` (ADR 0075) for this.** That runs *proto's* agent, which reaches
  protoAgent only as A2A tools. The operator wants to talk to *this* agent, with its
  memory, board, soul and fences. The two coexist.
- **Build (b) editor-routed writes now.** Deferred (D4).

## Consequences

- Any ACP client (Zed, JetBrains, Neovim, Emacs) can drive any protoAgent the operator can
  reach, with follow-the-agent into its files, and with no core change for read, search
  and follow.
- A new first-party package lives outside the core gates. Its tests
  (`integrations/zed-acp/tests`: tool mapping, a fake A2A server, a stdio subprocess
  round-trip) are **not** run by `checks.yml` yet. Adding a small CI leg is a follow-up.
- **Known Zed caveats.**
  - External agents don't get Zed Agent profiles, Zed Skills, or Zed's model picker. The
    model is gateway config (ADR 0033).
  - Review Changes doesn't apply to edits (D4).
  - Under SSH remoting, Zed runs the agent on the *remote* host. The shim then needs
    network access to the instance and `--root` mappings that are valid on that host.
    Relevant Zed issues: zed#47910 (registry agents on a remote server, closed), zed#60213
    (ACP agents not recovered after an SSH disconnect, **open**), and zed#52254 (remote
    ACP vs remote MCP, closed).
- **Cost.** A Zed thread is a full agent turn. protoEngineer answered one small question
  with ~190k input tokens (128k cached). Point Zed at a lean member for editor chat, not
  the PM.
- The ADR 0075 division of labour is amended: `proto` stays the coding-agent client and
  ACP server for *its* agent, and `protoagent-acp` is the ACP face of *protoAgent's* agent.

## Refs

Spike PR (`spike/zed-acp-shim`) · `integrations/zed-acp/` · ACP: protocol overview,
tool-calls, initialization, registry CONTRIBUTING (authMethods rule) · Zed docs
*External Agents* · ACP RFD *Streamable HTTP & WebSocket Transport* · ADR 0033 · ADR 0075 ·
ADR 0095 · ADR 0104
