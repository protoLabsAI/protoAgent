# Delegates — the agents & endpoints your agent can talk to

An **opt-in plugin** ([ADR 0025](/adr/0025-unified-delegate-registry-and-panel))
that gives the lead agent **one tool — `delegate_to(target, query)`** — over a
unified registry of delegates it can hand work to:

| `type` | What it is | Dispatch |
|---|---|---|
| **a2a** | A fleet **agent** over the A2A protocol | JSON-RPC `message/send` (+ poll) |
| **openai** | An OpenAI-compatible **model endpoint** — ask another model | `POST /v1/chat/completions` |
| **acp** | A CLI **coding agent** (protoCLI, Claude Code, …) over ACP | the ADR 0024 `AcpClient` |

This unifies what used to be three separate things — `peer_consult` (a2a),
`code_with` (acp), and "no way to ask another model" — into one hot-swappable
roster.

> **Where this is going:** PR1 (current) is **config-driven** — you declare
> delegates in YAML and they hot-reload on Save & Reload. A REST CRUD API (PR2)
> and a **console panel** to add/edit/test/remove delegates live (PR3) build on
> this. See [ADR 0025](/adr/0025-unified-delegate-registry-and-panel).

## Enable it

```yaml
# config/langgraph-config.yaml
plugins:
  enabled: [delegates]

delegates:
  - name: helm                      # the name the LLM passes to delegate_to(target=…)
    type: a2a
    description: Chief of staff — planning, fleet coordination.
    url: https://helm.example/a2a
    auth: { scheme: bearer }        # token from secrets.yaml (below) or *_env

  - name: opus
    type: openai
    description: Heavy reasoning model for deep analysis.
    url: https://api.proto-labs.ai/v1
    model: protolabs/reasoning
    system_prompt: "Answer thoroughly but concisely."

  - name: proto
    type: acp
    description: Terminal coding agent for this repo.
    command: proto
    args: ["--acp"]
    workdir: ~/dev/my-repo
    permissions: allowlist          # auto | allowlist | readonly (see ADR 0024)
```

`delegates` is a **top-level list** (ORBIS-style), not a plugin config section.
Editing it and hitting **Save & Reload** rebuilds the roster live — no restart
(protoAgent re-runs the plugin's `register()` with the new config).

## Use it

```
delegate_to(target="opus", query="What are the trade-offs of X vs Y? Be concise.")
delegate_to(target="proto", query="Add a /healthz route and run the tests.")
delegate_to(target="helm", query="What's the current sprint status?")
```

The configured delegate names + descriptions appear in the tool's description, so
the model knows what it can reach. Each delegate is stateless from the caller's
view — the `query` must be self-contained (the delegate doesn't see this chat).

## Secrets

Auth tokens / API keys are stored in the gitignored `config/secrets.yaml`, never
in the tracked config or in API responses — the same handling as the Discord /
Google tokens. For PR1 you can either:

- set the value in `secrets.yaml` (merged into the delegate at load), or
- reference an env var: `auth: { scheme: bearer, credentialsEnv: HELM_TOKEN }`
  (a2a) / `api_key_env: GATEWAY_KEY` (openai).

## Relationship to `code_with` / `peer_consult`

`delegate_to` supersedes them: an `acp` delegate is what `code_with` did, and an
`a2a` delegate is what `peer_consult` did. Both older tools still work for now and
will be deprecated once the panel lands (ADR 0025, PR4). New setups should prefer
`delegates` + `delegate_to`.
