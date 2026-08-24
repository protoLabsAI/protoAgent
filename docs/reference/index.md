# Reference

Information-oriented. Look up exact shapes and values here, grouped by **domain** (same order as the other sections).

## Agent core & runtime

| Page | Contents |
|---|---|
| [Configuration](/reference/configuration) | `config/langgraph-config.yaml` schema |
| [Environment variables](/reference/environment-variables) | Every env knob |

## Skills, subagents & workflows

| Page | Contents |
|---|---|
| [Skills (SKILL.md)](/reference/skills) | Frontmatter fields, source tags, the `skills:` config block |

## A2A, fleet & delegates

| Page | Contents |
|---|---|
| [A2A endpoints](/reference/a2a-endpoints) | JSON-RPC methods, SSE stream, well-known paths |
| [A2A conformance](/reference/a2a-conformance) | What of A2A 1.0 is implemented — methods, states, transports, gaps — plus the runnable prober |
| [Agent card](/reference/agent-card) | Card shape with every supported field |
| [Extensions](/reference/extensions) | A2A extensions — URI-keyed metadata (`cost-v1`, `worldstate-delta-v1`, `tool-call-v1`) + MIME-typed DataParts + `a2a.trace` |

## Tools, MCP & plugins

| Page | Contents |
|---|---|
| [Starter tools](/reference/starter-tools) | Shipped LangChain tools and their signatures |
| [Plugin manifest](/reference/plugin-manifest) | Every `protoagent.plugin.yaml` field — type, default, meaning |
| [Plugin registry API](/reference/plugin-registry-api) | The `register(registry)` surface: every seam, its signature, and `registry.host` |
| [Plugin SDK (`graph.sdk`)](/reference/plugin-sdk-api) | The stable capability surface — subagents, knowledge, watches, background jobs, metrics |
| [Plugin testkit](/reference/plugin-testkit) | The host-free harness: `load_plugin`, `FakeRegistry`, host stubs |
| [Plugin CLI](/reference/plugin-cli) | `python -m server plugin …` — scaffold, install, sync, uninstall |
| [Plugin view bridge](/reference/plugin-view-bridge) | The sandboxed-iframe postMessage protocol — every message, direction, payload, and trust rule |
| [Event bus topics](/reference/plugin-events) | Every topic core publishes, its payload keys, and the wildcard matching rules |

## Console & UI

| Page | Contents |
|---|---|
| [Operator REST API](/reference/operator-api) | The console's `/api/*` control-plane endpoints, grouped by area |
