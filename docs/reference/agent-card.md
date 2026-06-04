# Agent card

Served at `/.well-known/agent-card.json` and `/.well-known/agent.json`. Built by `server.py::_build_agent_card_proto` (which assembles it via `protolabs_a2a.build_agent_card`; skills come from `_SKILL_SPECS` / `_agent_skills()`).

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
      {"uri": "https://proto-labs.ai/a2a/ext/confidence-v1"},
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
> `protolabs_a2a` (not editable per-fork in `server.py`). Fork the `name`,
> `description`, `version`, and `skills`.

## Field reference

### `name`

Short agent identifier. Same value you pass via `AGENT_NAME`.

### `description`

One sentence. Used by planners and human consumers alike — write it for both audiences.

### `supportedInterfaces`

A2A 1.0 lists transports here (rather than a single top-level `url`). The template advertises one entry: `{url, protocolBinding: "JSONRPC", protocolVersion: "1.0"}`. The `url` must end with `/a2a` (the JSON-RPC endpoint, not the server root) — clients that strip the path and POST to `/` get a 405 from FastAPI.

### `version`

Your agent's version, not the A2A spec version. Semver is conventional.

### `capabilities`

| Key | What it means |
|---|---|
| `streaming: true` | `SendStreamingMessage` works — consumers switch to the SSE path |
| `pushNotifications: true` | `tasks/pushNotificationConfig/*` works — consumers can register webhooks |
| `extensions` | The four protoLabs DataPart extensions, declared by default — `cost-v1`, `confidence-v1`, `worldstate-delta-v1`, `tool-call-v1`. See [Extensions](/reference/extensions) |

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

- `id` — **sticky**. `cost-v1` samples, `worldstate-delta-v1` declarations, and Workstacean's routing all key on it. Don't rename.
- `tags` — free-form. Workstacean's planner does substring matching against goals.
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

## Fork this file

The card lives in `server.py::_build_agent_card_proto`. The template declares four custom extensions by default — **cost** / **confidence** / **worldstate-delta** / **tool-call** (the URIs come from `protolabs_a2a`; see [Extensions](/reference/extensions)). At a minimum, every fork should replace:

- `name` + `description`
- `skills` (sourced from `_SKILL_SPECS` / `_agent_skills()` — add your real ones)

## Related

- [Add a custom skill](/guides/add-a-skill) — walkthrough
- [A2A endpoints](/reference/a2a-endpoints) — methods callers use to reach skills
- [Extensions](/reference/extensions) — the extensions the template handles
