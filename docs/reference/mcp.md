# MCP connection and operator bridge reference

For app setup and recovery, use [Connect MCP servers](/guides/mcp). The operator bridge below exposes this instance to another client; it is separate from connecting an external tool server. Plugin factories are documented in [Build plugins](/guides/plugins#managed-mcp-servers-—-register-mcp-server).

## How long a call may run

`timeout_seconds` bounds **discovery**; `call_timeout_seconds` bounds a single
tool **invocation**. They are separate on purpose — connecting to a server should
be fast, but a legitimate call can be slow. `filesystem__search_files` over a
large root, for instance, walks the tree with no default exclusions (it does not
skip `node_modules` or `.git`) and no depth or result cap: one such call over a
2.75M-entry tree measured ~4 minutes. That is why the default is a generous 300s
— it is a backstop against a call that will *never* return, not a latency budget.

When it trips, the call is cancelled and the model gets a recoverable tool error
telling it to narrow the arguments; the turn is not failed. A server that
genuinely needs longer sets `call_timeout` on its own entry (or `0` to opt out):

```yaml
    - name: archiver
      transport: stdio
      command: /usr/local/bin/archiver-mcp
      call_timeout: 1800     # this one really does run for half an hour
```

If a call is being cut off that shouldn't be, prefer raising that server's
`call_timeout` over disabling the bound globally.

## How tools show up

- Each server's tools are **namespaced by server name**: a `read_file` tool on
  the `filesystem` server becomes **`filesystem__read_file`**. This prevents
  collisions with protoAgent's built-in tools (any that would still collide are
  skipped and logged).
- Tools are available to the **lead agent**. Subagents only get them if you add
  the namespaced name to that subagent's tool allowlist
  (see [Configure subagents](/guides/subagents)).
- `GET /api/runtime/status` reports `mcp.enabled`, the connected `servers`
  (`name`, `transport`, `tool_count`), and total `tool_count`.

## Keeping tools out of context (allowlist + lazy connect)

A single MCP server can export dozens or hundreds of tools, and bound tools can add names, descriptions, and input schemas to model context
unless deferred-tool loading is configured. Past ~10–15 tools this burns context and measurably degrades tool
selection ("tool pollution" — see [ADR 0005](/adr/0005-tool-pollution-and-progressive-disclosure)).
Two per-server knobs keep the surface small:

```yaml
mcp:
  enabled: true
  denylist: [dangerous__tool]    # cross-server hard block (always wins)
  servers:
    - name: github
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      tools:
        include: [get_pull_request, list_issues]   # allowlist — ONLY these bind
        exclude: [delete_repository]                # drop from whatever remains
    - name: staging-only
      enabled: false             # configured but not connected (no tools, no cost)
      transport: streamable_http
      url: "https://staging.example.com/mcp"
```

- **`tools.include`** — an allowlist. When set, *only* the listed tools are
  bound; everything else from that server is dropped. This is the surgical fix
  for a chatty server — pick the 2–10 tools you actually use.
- **`tools.exclude`** — drops the listed tools from whatever remains. `include`
  wins over a same-server `exclude` if a name appears in both.
- **`enabled: false`** — the server is **not connected at all** (lazy). Use it
  to park a server's config without paying its connection or context cost.
- Both `include`/`exclude` match the **bare** tool name (`get_pull_request`) or
  the namespaced form (`github__get_pull_request`).
- The global `denylist` is the hard safety net — it removes a tool even if an
  `include` lists it.

When no filter is set, all of a server's tools bind (the original behavior), so
existing configs are unchanged.

## Transports

| Transport | Use when | Required fields |
|---|---|---|
| `stdio` | Local tools / simple setups (server runs as a subprocess) | `command`, `args` (`env`, `cwd` optional) |
| `streamable_http` | Remote, production servers | `url` (`headers` optional) |
| `sse` | Legacy SSE servers | `url` (`headers` optional) |

::: tip `npx`/`node` not installed? (desktop)
Many stdio servers launch via `command: npx`. A desktop machine with no Node
toolchain has none to launch. Provision a managed one once —
`protoagent runtime install-node` (ADR 0085) — and every `npx`-based server (and
the [ACP coding agents](/guides/coding-agents)) can start. A Node you install
yourself wins when it's Node 20 or newer; an older one is shadowed by the managed runtime.
:::

## Sessions

Each server keeps **one long-lived MCP session** that every tool call reuses
(the default). This is how other MCP hosts (Claude Desktop, Cursor) drive
servers too, and it's what makes stdio servers fast: the stateless alternative
spawns a fresh subprocess **per call** (~1s of pure overhead for a typical
`npx`-launched server — an agent turn making ten calls would pay ten seconds).
Sessions open lazily on first use, and a server that dies mid-call is
reconnected **once** automatically; if that also fails the call returns a
recoverable tool-error string to the model instead of failing the turn.
Config reloads close the old sessions and open fresh ones.

If a specific server misbehaves when kept alive (leaks memory, caches stale
state), opt it out alone with `persistent: false` on its `servers` entry, or
set `mcp.persistent_sessions: false` to restore fresh-session-per-call
behavior globally:

```yaml
mcp:
  enabled: true
  persistent_sessions: true    # default — one live session per server
  servers:
    - name: flaky
      transport: stdio
      command: npx
      args: ["-y", "some-server"]
      persistent: false        # this server alone gets a fresh session per call
```

## Try it locally

A minimal stdio server ships at `examples/mcp/echo_server.py`:

```yaml
mcp:
  enabled: true
  servers:
    - name: echo
      transport: stdio
      command: python
      args: ["examples/mcp/echo_server.py"]
```

Start protoAgent and check `GET /api/runtime/status` — you'll see the `echo`
server with one tool (`echo__echo`).

## Expose THIS agent as an MCP server (operator tools)

The reverse direction (ADR 0033): publish this agent's own tools as an MCP server, so any
MCP client — Claude Desktop, Cursor, or an ACP coding-agent runtime — can **operate the
instance** (read/write notes & tasks, recall/ingest memory, run workflows, delegate to
subagents, set goals, schedule work). It's **opt-in + allowlist-gated** — only the tools you
name are exposed:

```yaml
operator_mcp:
  enabled: true
  tools: [memory_recall, memory_ingest, task_list, task_create, notes_read, run_workflow]
```

### Profiles (ADR 0075)

Instead of enumerating tools, pick a **profile** — a curated preset over the same allowlist:

```yaml
operator_mcp:
  profile: read-only        # reads/queries only, no state change
  # profile: full           # everything (≡ tools: ["*"])
  tools: [show_component]   # optional — a profile UNIONs with any explicit names
```

- **`read-only`** — a stable, principled set (recall/list/search/status; no writes).
- **`full`** — everything (`"*"`), minus the danger set below.
- **unset** (default) — deny-by-default: a foreign client gets *only* what `tools` names.
- The safe middle tier **`safe-operator`** (reads + non-destructive writes) lands with the ops
  layer (ADR 0075 D2), which carries per-op read/write metadata so it isn't a hand-maintained list.

**Env override:** `PROTOAGENT_MCP_TRUST=full` forces the `full` profile — for a trusted or
headless box where you vouch for the client (CI, your own machine). Per-op grants stay the
`tools` allowlist.

**Two tools are never exposed over MCP, even when named or via `"*"`:** `ask_human` and
`request_user_input` are HITL tools that pause the turn via an interrupt only the lead runner
resumes — over a foreign MCP client they'd hang it. (`execute_code` is dropped from `"*"` but
can be re-added by name — a coding-agent brain already has its own.)

Run it standalone:

```bash
python -m server.operator_mcp                 # stdio (for an MCP client / ACP session)
python -m server.operator_mcp --http --port 8848
```

**Core + plugin tools ride the same bridge** — a plugin's `register_tools` tools are exposed
through this one server (no per-plugin MCP); plugins that *are* an MCP server (`register_mcp_server`)
are mounted by the client directly. Empty `tools` ⇒ nothing exposed (don't hand `execute_code`
etc. to an outside brain unless you mean to). The sidecar boots **stores only** — it never starts
the agent's background loops — so it's safe to run against a live instance's data.

## Notes & limits

- **Tools only** for now — MCP *Resources* and *Prompts* aren't wired yet.
- Changing the `mcp` config is picked up on restart/reload, not hot-swapped
  per-request.
- Remote-server auth (OAuth 2.1) beyond static `headers` isn't handled yet —
  pass tokens via `headers` for now.
- Only enable servers you trust: their tools run with the agent's privileges.
