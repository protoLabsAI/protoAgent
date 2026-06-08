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

A **plugin** can contribute a managed MCP server (you never hand-edit
`mcp.servers`) via `register_mcp_server(factory)` — `factory(config)` returns an
`mcp.servers[]` entry, or `None` when the server shouldn't run (off / not yet
connected), so the server comes and goes with config. A plugin entry whose `name`
matches a configured server **replaces** it, and a plugin contributing a server
**activates MCP even when `mcp.enabled` is off**. The first-party **Google**
plugin (`plugins/google/`) is the worked example: an OAuth-gated Gmail/Calendar
server, launched frozen via `--mcp-plugin google`. See [Plugins](./plugins.md).

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

Each tool invocation opens a fresh MCP session and cleans up (the client is
stateless), so there's no long-lived connection to manage.

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
instance** (read/write notes & beads, recall/ingest memory, run workflows, delegate to
subagents, set goals, schedule work). It's **opt-in + allowlist-gated** — only the tools you
name are exposed:

```yaml
operator_mcp:
  enabled: true
  tools: [memory_recall, memory_ingest, beads_list, beads_create, notes_read, run_workflow]
```

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
