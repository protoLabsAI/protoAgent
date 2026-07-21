# Agent card

Served at `/.well-known/agent-card.json` and `/.well-known/agent.json`. Built by `server/a2a.py::_build_agent_card_proto` (which assembles it via `protolabs_a2a.build_agent_card`). Its identity is **config/plugin-driven** ([#570](configuration.md#a2a)) — `name` from `identity.name`, `description` + `skills` from the `a2a:` config section or `register_a2a_skill`, so a fork declares its card without editing the package.

## Full shape

The card is the **A2A 1.0** (`a2a-sdk` proto) shape, assembled by
`protolabs_a2a.build_agent_card`. A live card (`/.well-known/agent-card.json`):

```json
{
  "name": "my-agent",
  "description": "One-sentence statement of what this agent is for.",
  "supportedInterfaces": [
    {
      "url": "http://my-agent:7870/a2a",
      "protocolBinding": "JSONRPC",
      "protocolVersion": "1.0"
    }
  ],
  "provider": {
    "url": "https://protolabs.ai",
    "organization": "protoLabs AI"
  },
  "version": "0.2.1",
  "capabilities": {
    "streaming": true,
    "pushNotifications": true,
    "extensions": [
      {"uri": "https://proto-labs.ai/a2a/ext/cost-v1"},
      {"uri": "https://proto-labs.ai/a2a/ext/worldstate-delta-v1"},
      {"uri": "https://proto-labs.ai/a2a/ext/tool-call-v1"}
    ]
  },
  "securitySchemes": {
    "apiKey": {
      "apiKeySecurityScheme": {"location": "header", "name": "X-API-Key"}
    }
  },
  "securityRequirements": [
    {"schemes": {"apiKey": {}}}
  ],
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/markdown"],
  "skills": [
    {
      "id": "chat",
      "name": "Chat",
      "description": "General-purpose chat interface.",
      "tags": ["template"],
      "examples": ["hello", "what can you do?"]
    }
  ]
}
```

> The `provider` block, the four `capabilities.extensions`, and the
> `securitySchemes` / `securityRequirements` shapes are owned by
> `protolabs_a2a` (not editable per-fork). Customize `name` (`identity.name`),
> `description` + `skills` (the [`a2a:`](configuration.md#a2a) config section or
> `register_a2a_skill`), and `version` (your `pyproject`).

## Field reference

### `name`

Short agent identifier. Same value you pass via `AGENT_NAME`.

### `description`

One sentence. Used by planners and human consumers alike — write it for both audiences.

### `supportedInterfaces`

A2A 1.0 lists transports here (rather than a single top-level `url`). The template advertises one entry: `{url, protocolBinding: "JSONRPC", protocolVersion: "1.0"}`. The `url` must end with `/a2a` (the JSON-RPC endpoint, not the server root) — clients that strip the path and POST to `/` get a 405 from FastAPI.

The `url` is built by `_a2a_card_url()` as `{A2A_PUBLIC_URL}/a2a` (falling back to the bound loopback port when `A2A_PUBLIC_URL` is unset — correct for same-host local/desktop runs). **Set `A2A_PUBLIC_URL` to your externally-reachable address on any deployed agent**, and set `a2a.require_routable_url` to refuse boot rather than silently advertise a loopback URL no remote peer can dial.

**Fleet members** ([ADR 0042](/adr/0042-fleet-supervisor-unified-console)) get this for free: the supervisor spawns each member with `A2A_PUBLIC_URL = {hub_public_url}/agents/<slug>`, so a member's card advertises its own [tenant sub-path](/reference/a2a-endpoints#fleet-multi-tenancy-agents-slug) (`…/agents/<slug>/a2a`) — the [A2A URL-based routing](https://a2a-protocol.org/latest/topics/multi-tenancy/) contract — instead of inheriting the hub root (which every member would otherwise collide on, misrouting card-based discovery to the hub agent).

### `version`

Your agent's version, not the A2A spec version. Semver is conventional.

### `capabilities`

| Key | What it means |
|---|---|
| `streaming: true` | `SendStreamingMessage` works — consumers switch to the SSE path |
| `pushNotifications: true` | `tasks/pushNotificationConfig/*` works — consumers can register webhooks |
| `extensions` | The protoLabs extensions this agent actually emits — `cost-v1`, `worldstate-delta-v1`, `tool-call-v1` (URI-keyed `metadata`, not DataParts). `confidence-v1` is in the shared vocabulary but deliberately **not** declared, because nothing emits it. See [Extensions](/reference/extensions) |

Lying about capabilities breaks consumers silently. If you disable streaming (for example), also strip the handler routes — otherwise clients see a mismatch.

### `skills`

Each entry describes one dispatchable capability:

```json
{
  "id": "summarize_pr",
  "name": "Summarize Pull Request",
  "description": "Fetch a PR and return a three-bullet summary.",
  "tags": ["github", "summarization"],
  "examples": ["summarize https://github.com/..."],
  "inputModes": ["text/plain"],
  "outputModes": ["text/markdown"]
}
```

- `id` — **sticky**. `cost-v1` samples, `worldstate-delta-v1` declarations, and a consumer's routing all key on it. Don't rename.
- `tags` — free-form. A planner can do substring matching against goals.
- `examples` — few-shot-ish prompts consumers can surface in their UI.
- `inputModes` / `outputModes` — override `defaultInputModes` / `defaultOutputModes` for this specific skill.

### `defaultInputModes` / `defaultOutputModes`

MIME types the agent accepts/produces. Template ships `text/plain` in, `text/markdown` out.

### `securitySchemes` / `securityRequirements`

A2A 1.0 proto schemes. `apiKey` is **always** declared — an `X-API-Key` header
(`{"apiKeySecurityScheme": {"location": "header", "name": "X-API-Key"}}`), with a
matching `securityRequirements` entry (`{"schemes": {"apiKey": {}}}`).

Set the expected key value via the `<AGENT_NAME>_API_KEY` env var:

```bash
MY_AGENT_API_KEY=sk-abc123...
```

If the env var is unset, the API-key check is skipped entirely — useful for local dev, not appropriate for production.

When an A2A **bearer** token is configured (`auth.token` / `A2A_AUTH_TOKEN`), the
card *also* declares a `bearer` scheme (`{"httpAuthSecurityScheme": {"scheme":
"bearer"}}`) and appends it to `securityRequirements` as an OR-alternative — so a
consumer reading the card learns bearer is accepted, not just `apiKey`. (Both
shapes come from `protolabs_a2a.security_schemes(bearer=…)`.)

## Customize (no core edit)

The card is assembled in `server/a2a.py::_build_agent_card_proto`, but you **don't edit it** — identity is config/plugin-driven ([#570](configuration.md#a2a)). The template declares the three custom extensions it emits — **cost** / **worldstate-delta** / **tool-call** (URIs from `protolabs_a2a`, selected by `_emitted_extension_uris`; see [Extensions](/reference/extensions)). At a minimum, every fork sets:

- `name` → `identity.name` (the setup wizard sets it)
- `description` + `skills` → the [`a2a:`](configuration.md#a2a) config section (or a plugin's `register_a2a_skill`)

## Related

- [Add a custom skill](/guides/add-a-skill) — walkthrough
- [A2A endpoints](/reference/a2a-endpoints) — methods callers use to reach skills
- [Extensions](/reference/extensions) — the extensions the template handles
