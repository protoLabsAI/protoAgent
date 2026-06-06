# 0025 — Unified delegate registry + hot-swappable management panel

Status: **Accepted** (sliced; PR1 = backend registry)

## Context

protoAgent can already hand work to other agents three different ways, but they're
**split across three surfaces with three mental models**:

- **ACP coding agents** — `code_with` (the `coding_agent` plugin, ADR 0024),
  configured in `coding_agent.agents` YAML.
- **A2A peers** — `peer_consult` (`tools/peer_tools.py`), configured by
  `PEER_<HANDLE>_URL` **env vars only** (no UI, no per-call auth beyond a token).
- **Another model/endpoint** — not available as a delegate at all; the agent only
  has its own gateway model.

ORBIS (the protoLabs voice companion) solved this with a **single
`delegate_to(target, query)` tool over a unified delegate registry** with three
types — `a2a` / `openai` / `acp` — and a **React management panel** to add / edit /
test / remove delegates live. Each type is an adapter exposing a field schema
(drives a generic form), a `probe()` (the "Test" button), and a `dispatch()`.
Changes write `delegates.yaml` atomically, the registry reloads, and live sessions
re-register the tool — **hot-swap, no restart**.

The operator ask: *"I want to hot-swap the agents and endpoints my agent can talk
to."* — i.e. bring ORBIS's delegate registry + panel to protoAgent.

### What we already have that makes this cheap

protoAgent's **`_reload_langgraph_agent`** (Save & Reload / `/api/config/reload`)
already re-runs every plugin's `register()` with the **new config** and rebuilds
the graph's tools (`server/agent_init.py`). So config-driven tool config already
hot-swaps without a restart — that's the engine ORBIS's `registry.reload()` +
session-refresh provides. **What's missing is the unified registry, a CRUD API,
and the panel on top of it.**

## Decision

Build a **unified delegate registry** exposed as one `delegate_to(target, query)`
tool, plus a **hot-swappable management panel** in the operator console.

### Placement

- **Backend** — a first-party plugin **`plugins/delegates`** (claims the
  `delegates` config section, registers the `delegate_to` tool, and mounts the
  CRUD REST router via `register_router`). Keeps core stable (operator-fork
  contract) and rides the existing plugin hot-reload.
- **Panel** — lives in the **core React console** (`apps/web`), because a custom
  panel (type picker, Test button, health badges) is richer than the generic
  settings-schema form and must be part of the React build. It talks to the
  plugin's REST routes.

This **supersedes** the separate `coding_agent` plugin (acp) and the env-based
`peer_tools` (a2a): the acp adapter absorbs `code_with`'s `AcpClient`; the a2a
adapter absorbs `peer_consult`. `code_with` / `peer_consult` stay as thin,
deprecated back-compat shims for one release, then are removed.

### The model

```yaml
delegates:
  - name: helm                       # what the LLM passes to delegate_to(target=…)
    type: a2a                        # a2a | openai | acp
    description: Chief of staff — planning, fleet coordination.
    url: https://helm.example/a2a
    auth: { scheme: bearer }         # secret value lives in secrets.yaml (below)
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
    permissions: allowlist           # carried over from ADR 0024
```

One adapter per type, each providing:

- `config_schema()` → field specs (key/label/kind/required/help) that drive the
  generic panel form **and** server-side validation.
- `parse(raw) → Delegate` / `validate(raw)` — normalize + check.
- `probe(delegate) → {ok, latency_ms, error}` — the panel's **Test** button
  (a2a: GET agent-card; openai: `/v1/models` or a 1-token ping; acp: binary on
  PATH + workdir exists).
- `dispatch(delegate, query) → str` — the actual delegation (a2a: the
  `peer_tools` JSON-RPC+SSE path; openai: a `/v1/chat/completions` call; acp: the
  ADR 0024 `AcpClient`).

### Secrets (decision: inline → `secrets.yaml`)

The panel takes a secret **value** (bearer token, API key) in a field and routes
it to the gitignored `config/secrets.yaml` — the same handling as the Discord /
Google plugin tokens (ADR 0019). Keyed per delegate
(`delegates.<name>.<field>`), merged into the delegate config at load by the
existing `config_io` secret overlay. Secrets are **never** returned in API
responses or written to the tracked `langgraph-config.yaml`.

### Hot-swap

Reuses protoAgent's native path: a CRUD write updates `langgraph-config.yaml`
(delegates section) + `secrets.yaml`, then calls the existing reload
(`_reload_langgraph_agent`) — which re-runs the plugin's `register()` with the new
`delegates` config and rebuilds `delegate_to`. Next turn uses the new roster. No
restart. (Routes/surfaces still mount once — but the CRUD router is static; only
the *registry contents* change, which is config, not routes.)

### REST API (PR2)

Mounted by the plugin router:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/delegates` | list (+ `configured` flag + health) |
| POST | `/api/delegates` | create → write + reload |
| PUT | `/api/delegates/{name}` | update → write + reload |
| DELETE | `/api/delegates/{name}` | remove → write + reload |
| POST | `/api/delegates/test` | probe a (possibly unsaved) entry |
| GET | `/api/delegate-types` | type list + capabilities + field schema |

## Slices

- **PR1 (this ADR): backend registry + `delegate_to`.** `delegates` config
  section, the registry, the three adapters (a2a/openai/acp — acp reusing the ADR
  0024 `AcpClient`), the `delegate_to` tool, config-driven hot-reload, tests. No
  API/panel yet; configured via YAML. `code_with`/`peer_consult` untouched this
  slice.
- **PR2: CRUD REST API** — the endpoints above + atomic config/secrets writes +
  reload trigger + `probe()` for Test + `/delegate-types` schema.
- **PR3: React panel** — Delegates settings view in `apps/web` (type picker,
  generic form from the schema, Test button, list with status).
- **PR4: health prober** — background reachability loop + health badges; then
  deprecate `code_with`/`peer_consult` in favor of `delegate_to`.

## Consequences

- One mental model — *"manage the agents and endpoints my agent can talk to"* —
  with a live panel, replacing three disjoint mechanisms.
- A real openai-endpoint delegate (ask another model) that didn't exist before.
- Migration: `coding_agent.agents` → `delegates: [{type: acp, …}]`; `PEER_*_URL`
  env → `delegates: [{type: a2a, …}]`. Documented; shims ease the transition.
- The one core touch is the React panel (PR3); the rest is a plugin.

## Alternatives considered

- **Extend `coding_agent` only / agents+a2a only** — rejected by the operator in
  favor of full ORBIS parity (the panel should manage *endpoints* too).
- **Env-var-names-only secrets (ORBIS model)** — rejected in favor of inline →
  `secrets.yaml` for parity with the existing Discord/Google token UX.
- **All-in-core** — rejected; the backend fits the plugin seam (config + router +
  secrets), keeping core edits to just the React panel.
