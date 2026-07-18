# MCP (Model Context Protocol)

protoAgent can connect to external [MCP](https://modelcontextprotocol.io)
servers and expose **their tools as agent tools** — a standard way to plug in
filesystems, browsers, databases, SaaS APIs, and more without writing any
protoAgent-specific tool code. MCP is the same interop layer Claude Code,
Hermes, and OpenClaw speak, so the existing server ecosystem works out of the
box.

Built on [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters).

## Enabling it

MCP is **off by default** — configuring a server is the opt-in. The quickest way is
the console: **Agent → MCP → Add server** (name, transport, command/args or URL) wires
it in **without a restart** (the change hot-reloads and the server connects), and the
remove button drops one the same way. Got a config blob instead? Use **Paste JSON** —
it accepts the standard `{"mcpServers": {…}}` format (as shared by Claude Desktop / most
MCP docs), a single server object, or our own export, and imports them all at once.
Prefer YAML? Add an `mcp` section to your config
(`config/langgraph-config.yaml`, or via the wizard/drawer):

```yaml
mcp:
  enabled: true
  timeout_seconds: 20        # per-server discovery timeout
  denylist: []               # optional: drop specific (namespaced) tool names
  servers:
    # Local subprocess over stdio
    - name: filesystem
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
      env: {}                # optional
    # Remote server over streamable HTTP
    - name: weather
      transport: streamable_http
      url: "https://example.com/mcp"
      headers: {}            # optional (e.g. auth)
```

Servers are discovered at startup (and on config reload). A server that's
unreachable or errors is **logged and skipped** — it never blocks boot or the
other servers.

## How tools show up

- Each server's tools are **namespaced by server name**: a `read_file` tool on
  the `filesystem` server becomes **`filesystem__read_file`**. This prevents
  collisions with protoAgent's built-in tools (any that would still collide are
  skipped and logged).
- Tools are available to the **lead agent**. Subagents only get them if you add
  the namespaced name to that subagent's tool allowlist
  (`graph/subagents/config.py`).
- `GET /api/runtime/status` reports `mcp.enabled`, the connected `servers`
  (`name`, `transport`, `tool_count`), and total `tool_count`.

## Plugin-managed servers

A **plugin** can contribute a managed MCP server (you never hand-edit `mcp.servers`)
via `register_mcp_server(factory)` — `factory(config)` returns an `mcp.servers[]` entry,
or `None` when the server shouldn't run (off / not yet connected). The factory runs at
**every graph build** with the live `LangGraphConfig`, so the server comes and goes with
config. A plugin entry whose `name` matches a configured server **replaces** it, and a
plugin contributing a server **activates MCP even when `mcp.enabled` is off**.

### Worked example — wrap an external server, gated on config

In the plugin's `__init__.py` (`register(registry)` is the contribution hook — see
[Plugins](./plugins.md)):

```python
def register(registry):
    def mcp_factory(config):
        # registry.config is this plugin's resolved config section (ADR 0019).
        token = registry.config.get("api_token")
        if not token:
            return None                       # not configured → server doesn't start
        return {
            "name": "acme",                   # same name as a user mcp.servers entry → replaces it
            "transport": "stdio",             # or "streamable_http" / "sse"
            "command": "npx",
            "args": ["-y", "@acme/mcp-server"],
            "env": {"ACME_TOKEN": token},
        }

    registry.register_mcp_server(mcp_factory)
```

Enable the plugin and set its `api_token` (Settings ▸ Plugins, or its config section +
`secrets.yaml`); the `acme` tools appear (namespaced, allowlist-gated like any MCP server)
on the next graph build and vanish when the token is cleared. The returned dict is the same
`mcp.servers[]` entry shape as a hand-configured server (`command`/`args` + `env` for
stdio; `url`/`headers` for http/sse).

### A Python server inside the plugin (frozen apps)

If the plugin *implements* the server itself in Python (rather than wrapping an external
binary), it exposes an `mcp_main()` that runs the server, and the factory launches it as a
subprocess. In a **frozen desktop build** there's no `python` on PATH, so the agent
re-invokes its own binary with the generic **`--mcp-plugin <id>`** shim, which imports the
plugin and calls its `mcp_main()`. The first-party OAuth-gated Google (Gmail/Calendar)
plugin is the canonical example.

## Keeping tools out of context (allowlist + lazy connect)

A single MCP server can export dozens or hundreds of tools, and **every bound
tool's name, description, and full input schema is sent to the model on every
turn**. Past ~10–15 tools this burns context and measurably degrades tool
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
yourself always wins; this only fills the gap.
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

## Run on a coding agent (ACP runtime)

protoAgent can hand the *whole turn* to an external coding agent — **proto, Codex, Claude,
Copilot, OpenCode** — over ACP (ADR 0033). The coding agent is the brain (with its own
tools); protoAgent stays the shell (A2A, scheduling, goals, console, memory), and exposes
its operator tools to that brain via the MCP server above, mounted into the ACP session.

```yaml
agent_runtime: acp:proto         # native (default) | acp:<agent>
operator_mcp:
  tools: [memory_recall, memory_ingest, task_create, task_list, notes_read, run_workflow]
# acp:                           # optional — override an agent's launch command
#   agents:
#     codex: { command: npx, args: ["-y", "@zed-industries/codex-acp"] }
```

With this set, each turn is driven by the coding agent: protoAgent assembles the context
(a cacheable persona prefix sent once, then per-turn deltas — ADR 0033 D5), the agent reasons
+ uses its own tools, and reaches back into protoAgent's notes/tasks/memory/workflows through
the mounted operator MCP server. Defaults are `native` (the built-in LangGraph loop), so this
is inert until you opt in. Each agent needs its CLI installed + authenticated on the host.

## Notes & limits

- **Tools only** for now — MCP *Resources* and *Prompts* aren't wired yet.
- Changing the `mcp` config is picked up on restart/reload, not hot-swapped
  per-request.
- Remote-server auth (OAuth 2.1) beyond static `headers` isn't handled yet —
  pass tokens via `headers` for now.
- Only enable servers you trust: their tools run with the agent's privileges.
