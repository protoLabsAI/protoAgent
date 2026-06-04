# Agent card

Served at `/.well-known/agent-card.json` and `/.well-known/agent.json`. Built by `server.py::_build_agent_card_proto` (which assembles it via `protolabs_a2a.build_agent_card`; skills come from `_SKILL_SPECS` / `_agent_skills()`).

## Full shape

```json
{
  "name": "my-agent",
  "description": "One-sentence statement of what this agent is for.",
  "url": "http://my-agent:7870/a2a",
  "version": "0.2.1",
  "provider": {
    "organization": "protoLabsAI",
    "url": "https://github.com/protoLabsAI"
  },
  "capabilities": {
    "streaming": true,
    "pushNotifications": true,
    "stateTransitionHistory": false,
    "extensions": [
      {"uri": "https://proto-labs.ai/a2a/ext/cost-v1"},
      {"uri": "https://proto-labs.ai/a2a/ext/confidence-v1"}
    ]
  },
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
  ],
  "securitySchemes": {
    "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
  }
}
```

## Field reference

### `name`

Short agent identifier. Same value you pass via `AGENT_NAME`.

### `description`

One sentence. Used by planners and human consumers alike — write it for both audiences.

### `url`

Must end with `/a2a`. The spec field points at the JSON-RPC endpoint, not the server root. Clients that use `/a2a` as-is are fine; clients that strip the path and POST to `/` get a 405 from FastAPI.

### `version`

Your agent's version, not the A2A spec version. Semver is conventional.

### `capabilities`

| Key | What it means |
|---|---|
| `streaming: true` | `message/stream` works — consumers switch to the SSE path |
| `pushNotifications: true` | `tasks/pushNotificationConfig/*` works — consumers can register webhooks |
| `stateTransitionHistory` | Returns historical state per task. Template defaults to `false` |
| `extensions` | See [Extensions](/reference/extensions) |

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

- `id` — **sticky**. `cost-v1` samples, `effect-domain-v1` declarations, and Workstacean's routing all key on it. Don't rename.
- `tags` — free-form. Workstacean's planner does substring matching against goals.
- `examples` — few-shot-ish prompts consumers can surface in their UI.
- `inputModes` / `outputModes` — override `defaultInputModes` / `defaultOutputModes` for this specific skill.

### `defaultInputModes` / `defaultOutputModes`

MIME types the agent accepts/produces. Template ships `text/plain` in, `text/markdown` out.

### `securitySchemes`

Standard OpenAPI-style security scheme declaration. The template's default is an `X-API-Key` header.

Set the expected value via `<AGENT_NAME>_API_KEY` env var:

```bash
MY_AGENT_API_KEY=sk-abc123...
```

If the env var is unset, the API key check is skipped entirely — useful for local dev, not appropriate for production.

When an A2A bearer token is configured (`auth.token` / `A2A_AUTH_TOKEN`), the
card adds a `bearer` scheme **and** lists it in the `security` requirement
alongside `apiKey` (`[{"apiKey": []}, {"bearer": []}]` — either is accepted), so
a consumer reading the card actually learns bearer is an option rather than
seeing only `apiKey`.

## Fork this file

The card lives in `server.py::_build_agent_card_proto`. The template declares four custom extensions by default — **cost** / **confidence** / **worldstate-delta** / **tool-call** (the URIs come from `protolabs_a2a`; see [Extensions](/reference/extensions)). At a minimum, every fork should replace:

- `name` + `description`
- `skills` (sourced from `_SKILL_SPECS` / `_agent_skills()` — add your real ones)

## Related

- [Add a custom skill](/guides/add-a-skill) — walkthrough
- [A2A endpoints](/reference/a2a-endpoints) — methods callers use to reach skills
- [Extensions](/reference/extensions) — the extensions the template handles
