# Connect MCP servers

Connect an MCP server to give the agent tools for a service, database, browser,
or other external system. You need the server’s launch command or URL and any
credentials it requires. Enable only servers you trust; a subprocess is not an
OS sandbox.

## Add a server

1. Open **Settings → MCP**. Use the catalog for a listed integration, or **Add server**.
2. For a local `stdio` server, enter its command and arguments. The command must
   be available on the machine running protoAgent. For `http` or `sse`, enter
   the server URL; `localhost` refers to that machine, not your browser.
3. If the provider supplies configuration, choose **Paste JSON**. It accepts a
   standard `mcpServers` object or a single server definition. Include required
   environment values or headers in the supplied configuration.
4. Submit, then check that the server appears with its tool count. Changes
   apply without a restart. A connected server does not prove every tool can
   access its target service.
5. Ask for a small read-only task using that integration and inspect the tool
   result. Names are prefixed with the server name, such as `weather__forecast`.

For filesystem access, prefer the built-in
[work-folder tools](/guides/react-tauri-ui#working-memory-the-filesystem-fence).
For `npx` servers on desktop, install Node from the missing-runtime card when
prompted, or use the [coding-agent runtime guidance](/guides/coding-agents).

## Fix a connection or failed call

- **Command missing:** check its installation and the server process’s PATH;
  an absolute command path can remove ambiguity.
- **URL or authentication error:** confirm the endpoint and credentials in the
  server definition. Check access from protoAgent’s machine.
- **Connected, but no useful tools:** inspect the tool count and configured
  include/exclude filters. A subagent also needs the tool in its allowlist.
- **Tool timeout:** narrow the task or increase that server’s call timeout when
  the work legitimately needs longer. Discovery and invocation have separate
  timeouts; see [MCP reference](/reference/mcp#how-long-a-call-may-run).

Keep the error message. A missing server may have failed discovery; inspect the
agent’s log before repeatedly adding it. Remote OAuth sign-in is not handled by
this connector; use the server’s supported static headers where applicable.

## Stop using a server

Use its remove button in **Settings → MCP**. For a config-managed server, set
`enabled: false` on its entry and reload configuration to retain the definition
without connecting. Plugin-managed servers must be disabled through the plugin
or its configuration, otherwise the plugin can contribute them again.

## Configuration and extension paths

- [MCP configuration](/reference/configuration#mcp) — YAML fields.
- [MCP reference](/reference/mcp) — filtering, transports, sessions, timeouts,
  and exposing this agent’s operator tools to another client.
- [Build plugins](/guides/plugins#managed-mcp-servers-—-register-mcp-server) — contribute a managed MCP server.
- [Deprecated ACP runtime](/guides/acp-runtime) — existing configurations only;
  use [coding delegates](/guides/coding-agents) for new coding work.

## Earlier section links

<a id="mcp-model-context-protocol"></a>
<a id="enabling-it"></a>
<a id="how-long-a-call-may-run"></a>
<a id="how-tools-show-up"></a>
<a id="plugin-managed-servers"></a>
<a id="worked-example-—-wrap-an-external-server-gated-on-config"></a>
<a id="a-python-server-inside-the-plugin-frozen-apps"></a>
<a id="keeping-tools-out-of-context-allowlist-lazy-connect"></a>
<a id="transports"></a>
<a id="sessions"></a>
<a id="try-it-locally"></a>
<a id="expose-this-agent-as-an-mcp-server-operator-tools"></a>
<a id="profiles-adr-0075"></a>
<a id="run-on-a-coding-agent-acp-runtime-—-deprecated"></a>
<a id="notes-limits"></a>

Earlier advanced sections are in [MCP reference](/reference/mcp), [plugin authoring](/guides/plugins#managed-mcp-servers-—-register-mcp-server), and [ACP migration guidance](/guides/acp-runtime).
