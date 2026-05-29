# MCP (Model Context Protocol)

protoAgent can connect to external [MCP](https://modelcontextprotocol.io)
servers and expose **their tools as agent tools** — a standard way to plug in
filesystems, browsers, databases, SaaS APIs, and more without writing any
protoAgent-specific tool code. MCP is the same interop layer Claude Code,
Hermes, and OpenClaw speak, so the existing server ecosystem works out of the
box.

Built on [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters).

## Enabling it

MCP is **off by default** — configuring a server is the opt-in. Add an `mcp`
section to your config (`config/langgraph-config.yaml`, or via the wizard/drawer):

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

## Notes & limits

- **Tools only** for now — MCP *Resources* and *Prompts* aren't wired yet.
- Changing the `mcp` config is picked up on restart/reload, not hot-swapped
  per-request.
- Remote-server auth (OAuth 2.1) beyond static `headers` isn't handled yet —
  pass tokens via `headers` for now.
- Only enable servers you trust: their tools run with the agent's privileges.
