# Configuration

The live config is `<instance_root>/config/langgraph-config.yaml`. All fields
have defaults; YAML overrides the values you change. Use **Settings** to edit the
running agent, or locate its files and the source of each effective value with:

```bash
protoagent config explain
# Source checkout:
uv run python -m server config explain
```

## File locations

| Installation | Instance root | Live config |
| --- | --- | --- |
| Source or Python package, default instance | `~/.protoagent/default` | `~/.protoagent/default/config/langgraph-config.yaml` |
| Named host instance | `<box_root>/<PROTOAGENT_INSTANCE>` | `<instance_root>/config/langgraph-config.yaml` |
| Packaged desktop | The app's per-user config directory | `<instance_root>/config/langgraph-config.yaml` |
| Bundled Docker image | `/sandbox` | `/sandbox/config/langgraph-config.yaml` |

`PROTOAGENT_HOME` sets an explicit instance root. Otherwise, `PROTOAGENT_INSTANCE`
selects a directory under `PROTOAGENT_BOX_ROOT` (normally `~/.protoagent`, or
`/sandbox` in Docker). With neither set, the instance is `default`.
Identity resolves from environment variables before config loads; `instance.id`
in YAML does not select the data root. See [Run multiple instances](/guides/multi-instance).

The repository's `config/langgraph-config.example.yaml` is a read-only seed.
First boot creates the live config under the instance root. Persona (`SOUL.md`),
secrets (`secrets.yaml`), and the setup marker (`.setup-complete`) are siblings of
that file. In examples below, `config/` means this instance's live config directory.

Machine-shared defaults live in `<box_root>/host-config.yaml`. Settings resolve
through the App → Host → Agent cascade; `config explain` shows provenance and
redacts secrets.

<!-- docs-media: config-precedence -->

<!-- /docs-media -->

## Full example

```yaml
providers:
  - id: gateway
    type: openai-compat
    base_url: http://gateway:4000/v1   # its key lives in secrets.yaml → providers.gateway

model:
  name: gateway:protolabs/reasoning
  temperature: 0.2
  max_tokens: 32768
  max_iterations: 2000

subagents:
  researcher:
    enabled: true
    tools:
      - current_time
      - web_search
      - fetch_url
      - memory_recall
      - memory_list
    max_turns: 40

middleware:
  knowledge: true
  audit: true
  memory: true
  scheduler: true

knowledge:
  db_path: /sandbox/knowledge/agent.db
  embed_model: nomic-embed-text
  top_k: 5
```

## `providers`

The model connections ([ADR 0106](/adr/0106-provider-registry)): one entry per endpoint or
subscription the agent can reach. Managed in **Settings ▸ Model ▸ Connections**; every model
slot names a model *within* one of them.

| Key | What |
|---|---|
| `id` | Slug, `[a-z0-9][a-z0-9_-]*`, **immutable** — stored model values name it (`<id>:<model>`), including in other instances' configs via the Host layer. Rename = remove and re-add. |
| `type` | `openai-compat` (a LiteLLM gateway, vLLM, Ollama, LM Studio …), `anthropic-oauth` (Claude Pro/Max) or `openai-codex` (ChatGPT/Codex plan). Several entries may share a type. |
| `label` | Display name only; freely editable. |
| `base_url` | The endpoint, for `openai-compat`. Subscriptions have none. |
| `api_key` | **Secret — not stored here.** Kept in `config/secrets.yaml` under `providers: {<id>: …}` (see [Secrets](#secrets)). A keyless endpoint (a local vLLM) is normal. |

```yaml
providers:
  - id: gateway
    type: openai-compat
    base_url: https://api.proto-labs.ai/v1
  - id: local
    type: openai-compat
    base_url: http://127.0.0.1:8080/v1
  - id: anthropic-oauth
    type: anthropic-oauth
model:
  name: gateway:protolabs/reasoning
```

- **Box-shared, per-field.** A Host-layer (`host-config.yaml`) list is inherited by every
  instance on the box; an instance entry with the same `id` overrides it **field by field**.
  So leave a field out to inherit it — an empty `base_url: ""` *replaces* the box endpoint
  rather than deferring to it. Keys never come from the Host layer.
- **A connection is resolved strictly from its own fields.** Its key is never borrowed from
  another connection, from `model.api_key` or from `OPENAI_API_KEY`.
- **No `providers:` block** (the shipped template) means the loader builds the registry the
  legacy fields imply: a `gateway` entry from `model.api_base` (App default
  `http://gateway:4000/v1`) keyed from `model.api_key` or `OPENAI_API_KEY`, plus the lead
  subscription when `model.provider` names one. **`providers: []`** is different — it is what
  removing the last connection writes, and it stays empty.

## `model`

| Key | Default | What |
|---|---|---|
| `name` | `protolabs/reasoning` | The lead model. `<id>:<model>` names the connection that serves it (`gateway:protolabs/reasoning`, `anthropic-oauth:claude-sonnet-5`); a bare gateway alias routes to the gateway. |
| `temperature` | `0.2` | Sampling temperature. |
| `max_tokens` | `32768` | Per-call output cap. 32k headroom for the Qwen models we run. |
| `max_iterations` | `2000` | LangGraph `recursion_limit` for the tool loop — counts graph steps through the middleware chain, not tool calls 1:1 (~8 steps/tool call, so 2000 ≈ 250 tool calls/turn). |
| `favorites` | `[]` | Pinned go-to models for the chat `/model` quick-switch — the inline picker offers these, in this order, instead of the gateway's full list. Manage (add/remove/reorder) in Settings ▸ Model ▸ Favorite models. Empty = `/model` shows every gateway model with a hint to pin favorites. |
| `request_timeout` | `120.0` | Per-call gateway timeout (seconds) — bounds a hung/slow gateway so a turn fails cleanly. |
| `max_retries` | `2` | Transient-retry cap on the LLM client (→ `llm_max_retries`). |
| `max_inflight` | `0` | Cap on concurrent model calls per lane (resolved endpoint + model) in **this process**. `0` (default) disables the in-flight limiter entirely. Per-process, not box-wide — size the limits across your instances so they sum within the gateway's parallel capacity. [ADR 0115](../adr/0115-gateway-inflight-limiter.md); see [Operate the model in-flight limiter](../guides/model-concurrency.md). |
| `inflight_queue_timeout` | `300` | Longest one caller may wait (seconds) for a free slot when the lane is saturated. **Separate from `request_timeout`** (which times the HTTP call once a slot is held); kept below the turn stall timeout so a queued turn fails with a queue error, not a stall. Only applies when `max_inflight` is non-zero. |
| `inflight_interactive_reserve` | `1` | Slots reserved for `interactive` (operator-watched) callers, so a background fan-out can't take the operator's last slot. Clamped to `max_inflight − 1`, leaving non-interactive work at least one slot. Per-process like `max_inflight`; only applies when the limiter is on. |
| `top_p` | _(unset)_ | Nucleus sampling. Standard OpenAI param; sent only when set. |
| `presence_penalty` | _(unset)_ | Standard OpenAI param; sent only when set. |
| `top_k` | `-1` | Top-k sampling. Rides `extra_body` (vLLM-style gateways). `-1`/negative = gateway default. |
| `repetition_penalty` | _(unset)_ | Rides `extra_body`; sent only when set. |
| `chat_template_kwargs` | _(unset)_ | Dict passed via `extra_body` to the vLLM renderer, e.g. `{preserve_thinking: true}` to keep historical `<think>`/`<scratch_pad>` blocks across turns. |

All sampling params are optional — omit to use the gateway / model-card defaults. `temperature`, `max_tokens`, `top_p`, and `presence_penalty` are standard OpenAI fields; `top_k`, `repetition_penalty`, and `chat_template_kwargs` are sent via `extra_body` for vLLM-compatible gateways.

**Legacy compatibility: `model.provider`, `model.api_base`, `model.api_key`.**
Use the `providers:` registry for new configuration; Settings exposes the registry.
Legacy fields remain supported when loading older configuration without a `providers:`
block. The removal proposed in #3128 is deferred; no removal version is scheduled.
This retains compatibility readers and import/upgrade paths, not a second recommended
configuration format. Declared API-key connections do not borrow unrelated environment credentials, and explicit `providers: []` resolves no model lanes.

## Secrets

The **core** secrets are **never written to the tracked config YAML**: each model connection's key (`providers.<id>`; the legacy `model.api_key` on a config with no `providers:` block), the A2A `auth.token`, and the A2A `auth.federation_token` (ADR 0066). (Plugins may declare more — e.g. `discord.bot_token`, `google.client_secret` — which are routed and stripped the same way via a dynamic `secret_paths()`; ADR 0019.) The setup wizard and settings drawer persist them to an **untracked** sibling file, `config/secrets.yaml` (gitignored, dockerignored, written `0600`):

```yaml
# config/secrets.yaml — never committed
providers:
  gateway: sk-...             # the key of the `gateway` connection (one line per keyed id)
auth:
  token: bearer-...
  federation_token: fed-...   # optional (ADR 0066) — semi-trusted peers get THIS, not `token`
```

`LangGraphConfig.from_yaml` overlays this file on top of the main config at load time. Precedence for each secret: **`secrets.yaml` → main YAML value → env var** (`OPENAI_API_KEY` / `A2A_AUTH_TOKEN`). A declared connection's key has no env tier — `OPENAI_API_KEY` only feeds the `gateway` the loader builds for a config with no `providers:` block. So env-injected deployments (e.g. `infisical run`) work unchanged — just leave `secrets.yaml` absent. Every config save also strips any secret keys the main YAML might still carry, so a checkout converges to secret-free — and the strip **relocates, never drops**: an inline value `secrets.yaml` doesn't already hold (e.g. a hand-seeded `model.api_key` on a fresh instance with no secrets file yet) is written to the overlay in the same save, an existing overlay value is never overwritten by a stale inline copy, and if the overlay write fails the key stays inline rather than being lost (#1645). The `/api/config` endpoint redacts all three fields to `""`; runtime status reports only whether a key is set (`model.api_key_configured`), never the value. (`auth.federation_token` doesn't share `from_yaml`'s env-var fallback above — its own env source, `A2A_FEDERATION_TOKEN`, is read one layer down, by the A2A auth guard itself; see "Federation token" below.)

### External secrets manager (ADR 0080) {#secrets_manager}

Instead of hand-maintaining env vars (or wrapping the process in `infisical run`), the server can **pull secrets from Infisical itself** and export them as env vars — at boot, on every config reload, and on a refresh interval, so rotation lands without a restart. Enable it with the `secrets_manager` section (or Settings ▸ Secrets manager, which includes a connection test and a sync-now button):

```yaml
secrets_manager:
  enabled: true
  provider: infisical
  host: https://us.infisical.com   # or your self-hosted URL
  project_id: "<project id>"
  environment: prod                # the dev sandbox instance can point at `dev`
  path: /
  recursive: true
  refresh_seconds: 300             # 0 = fetch only at boot / config reload
  required: false                  # true = refuse to boot when the manager is unreachable
  override_env: false              # true = manager values beat pre-existing env vars
```

Behavior:

- **Fetched values land in the env fallback tier.** The precedence above is unchanged: `secrets.yaml` → main YAML → env; the manager merely populates env. An env var you exported yourself always shadows the manager (flip `override_env` for rotation-wins), and only vars the hydrator *set itself* are ever updated or removed on refresh.
- **Bootstrap credentials stay local** — the universal-auth machine identity (`client_id` / `client_secret`) is the one pair that can't come from the manager. It resolves like any core secret: `secrets_manager.client_id/client_secret` in `secrets.yaml` (where the Settings UI stores them, never echoed back) → main YAML → the `INFISICAL_CLIENT_ID` / `INFISICAL_CLIENT_SECRET` env vars. The recommended posture: *secrets.yaml holds only the machine identity; every other credential lives in the manager.* Fetched values can never overwrite the bootstrap pair or `PROTOAGENT_*` instance identity.
- **Failures don't take the boot down** — a fetch error logs one warning and the server continues with whatever the env already has; set `required: true` to fail fast instead. `PROTOAGENT_NO_SECRETS_HYDRATE=1` disables hydration entirely (debugging escape hatch). Nothing is ever cached to disk.
- **Operator surface:** `GET /api/secrets/status` (last outcome + which env vars are manager-owned — names, never values), `POST /api/secrets/sync`, `POST /api/secrets/test`.
- Scope the machine identity least-privilege in Infisical (that project, that environment, read-only). Values are also registered with the audit-log redaction layer, so a manager-sourced credential echoed by a tool is scrubbed by exact match.

### Federation token — operator vs peer (ADR 0066)

A deployment that hands its `/a2a` endpoint to **semi-trusted A2A peers** (a fleet hub, a partner agent) can issue them a **second** credential instead of the operator bearer: set `auth.federation_token` (secret; env fallback `A2A_FEDERATION_TOKEN`). A request authenticated with the federation token reaches only the **`/a2a` + `/v1` consumer surfaces** — it is **denied the whole `/api` operator surface** (plugin install/enable = host code-exec, config/SOUL rewrite, subagent runs, the operator goal set-path) with a `403`. The operator bearer keeps full access. Leaving `federation_token` blank is **single-token mode** (unchanged behavior): every bearer holder is the operator.

This is the R1 path ceiling behind the goal trust-gate: the dangerous goal verifiers (`command`/`test`/`ci`, `data`+`expr`) are refused from a `/goal` **chat** message for everyone (Phase 1), and the operator sets them through the operator-tier **`POST /api/goals`** endpoint — safe precisely because the `/api` ceiling confines that endpoint to the operator.

**Setting a federation token protects nothing by itself** — every peer that already holds your operator bearer keeps working exactly as before, since the operator bearer is still accepted everywhere. It only starts to matter once each peer is rotated onto the federation credential instead:

1. **Issue the token.** Set `auth.federation_token` on the RECEIVING agent (Settings ▸ Identity, or `secrets.yaml` / `A2A_FEDERATION_TOKEN` directly) — either an existing high-entropy secret or a freshly generated one. It applies live, no restart.
2. **Rotate each SENDING peer's delegate config onto it.** On the peer that calls this agent, find the `delegates` entry (or fleet-member registration) pointing at this agent's `/a2a` URL and replace its `auth.token` value with the SAME string you just set as `auth.federation_token` here. The receiving side classifies by which secret matched, not by any declared scheme — no other config change needed. See [Declare delegates](../guides/delegates.md#declare-delegates).
3. **Verify before you rely on it.** Confirm the rotated peer can still reach `/a2a` (a normal `delegate_to`/task call succeeds) and that it now gets `403` if it tries an `/api` operator-only endpoint directly — that second check is the actual security property; a peer that still holds the operator bearer somewhere else would silently pass the first check without it.
4. **Track which peers are done.** There's no built-in registry of "who's rotated yet" — until you've done this for a given peer, it still holds the operator credential and the federation token buys nothing for that specific relationship. Treat an unrotated peer as fully trusted with `/api`, because it still is.

A stricter mode that outright *refuses* the operator bearer on `/a2a` once a federation token is configured has been discussed but is deliberately **not implemented**: the console's own chat UI streams over `/a2a` using the operator bearer, so a naive version of that flag would lock the operator out of their own console the moment they configure a federation token, and there's no reliable, non-forgeable way to tell "the console" apart from "an external peer" at that layer (see ADR 0066's R5). Rotation today is voluntary and per-peer, not enforced.

### Credential-plane keys (ADR 0121) {#credential-plane-keys}

A paired phone or tablet has **`device` scope** ([ADR 0121](../adr/0121-device-pairing-over-an-operator-supplied-public-url.md) D4): it can change most settings, but not the ones that decide who can get in or where the tool sandbox ends. Writing any of these from a `device`-scope credential (Settings, `POST /api/settings`, `/api/settings/reset`, `POST /api/config` or the setup wizard) is refused with `403 device_scope`, and the response names the key. Other keys in the same save are not applied either, so nothing is half-saved. A full-access credential (the auth token, the desktop app, an agent's hub, a promoted device) can write them as usual.

| Key | Why |
|---|---|
| `auth.*` — `auth.token`, `auth.federation_token` | The credentials themselves |
| `network.*` — `network.bind` and any `network.` key added later (such as `network.trusted_proxies` and `network.public_console_url`) | Where the agent listens and how it's reached. Matched by prefix, so a new key is covered the day it lands |
| `devices.*` | Device-pairing policy |
| `egress.*` — `egress.allowed_hosts` | The outbound half of the tool sandbox |
| `filesystem.allow_run` | Whether the agent can run shell commands at all |
| `secrets_manager.*` | Where secrets (the auth token's environment fallback among them) are loaded from; the config side of `/api/secrets/*` |
| `plugins.*` | The plugin install/enable lists; the config side of the plugin-lifecycle routes |
| `mcp.*`, `delegates`, `lifecycle_hooks`, `acp.*`, `filesystem.editor_command` | Commands the server runs (MCP servers, delegate and ACP agent executables, event hooks, the open-in-editor command). A phone that set one would leave host code behind that outlives its revoke; the config side of the `/api/mcp/servers` routes |

`A2A_ALLOWED_ORIGINS` and `PROTOAGENT_TRUSTED_HOSTS` are environment variables with no config key, so no config write can reach them. The Settings schema marks each of these fields with `credential_plane: true`.

## `subagents`

The built-in `subagents.researcher` block accepts the fields below. Plugin roles are registered with `register_subagent`; arbitrary YAML names do not create new roles. Defaults shown are for the built-in researcher.

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | If false, the subagent is still registered but dispatches return "disabled" errors. |
| `tools` | researcher tool list | Allowlist; see the researcher example in [Configure subagents](/guides/subagents#_3-add-to-the-yaml). Tool names not listed are unavailable to this role. |
| `max_turns` | `40` | Tool rounds per delegation: the subagent can call tools this many times, then it must answer. The runner converts this into LangGraph's step-based `recursion_limit` using the compiled middleware stack, so adding middleware never shrinks it. Round `max_turns + 1` hard-stops, and the partial output comes back marked `hard-stopped at max_turns`. |
| `model` | `""` | Pin this subagent to a model. A subagent run picks its model in this order: this pin, then the delegating turn's model override (the chat tab's pick, `metadata.model`), then `routing.aux_model`, then the main model. The same order covers `task` / `task_batch`, `/<subagent>` slash runs and background jobs. A background job runs as a detached turn, so it takes the pin or the override when one is set and otherwise runs on the main model. |

Two `subagents`-block keys govern **fan-out** via the `task_batch` tool (concurrent delegation):

| Key | Default | What |
|---|---|---|
| `max_concurrency` | `4` | Cap on in-flight subagents per `task_batch` call (protects the gateway + context budget). |
| `output_truncate` | `6000` | Per-subagent returned-text cap (chars) under `task_batch`, so a wide fan-out can't blow the parent context. Single `task` is unbounded. |

```yaml
subagents:
  max_concurrency: 4
  output_truncate: 6000
  researcher:
    enabled: true
    tools: [...]
```

For a new role, use a plugin and its declared settings. See [Configure subagents](/guides/subagents) for registration and verification without editing core.

## `middleware`

| Key | Default | What |
|---|---|---|
| `knowledge` | `true` | Inject retrieved knowledge into state before LLM calls. Backed by the bundled `KnowledgeStore` (sqlite + FTS5). Set `false` for a stateless agent. |
| `audit` | `true` | Append every tool call to `/sandbox/audit/audit.jsonl`. |
| `memory` | `true` | Persist a reasoning-stripped session summary at terminal turn / session end (read back as `<prior_sessions>` by the knowledge middleware). |
| `scheduler` | `true` | Wire the bundled scheduler backend (local sqlite). Drops the `schedule_task` / `list_schedules` / `cancel_schedule` tools from the agent loop when `false`. Has the same effect as `SCHEDULER_DISABLED=1` — but `middleware.scheduler: false` is the canonical opt-out (drawer/wizard editable, survives restarts), while the env var is a runtime escape hatch for fleet operators who can't edit YAML in the moment. |
| `enforcement` | `false` | Opt-in safety gate that blocks tool calls **before** they execute (see `enforcement` block below). **YAML / code seam — not surfaced in the console**, since it's a no-op until a deny list, rate limit, or predicate is configured. |

## `enforcement`

Optional pre-execution gate (`graph/middleware/enforcement.py`). Only read when `middleware.enforcement: true`. Blocked calls return a `ToolMessage` explaining the denial (the model reads it and adapts) instead of running the tool. Forks needing richer policy (scope/cost/etc.) can attach a `predicate(tool_name, args) -> reason|None` in code.

This is intentionally a **fork seam**, not a console feature: the bare `middleware.enforcement` toggle is hidden from the operator settings UI (it does nothing until you add a `disallowed_tools` list, `rate_limits`, or a predicate here), so configure it in YAML / code.

```yaml
middleware:
  enforcement: true
enforcement:
  disallowed_tools: [fetch_url]          # exact names never allowed
  rate_limits:
    web_search: { max: 20, window_seconds: 60 }
```

| Key | Default | What |
|---|---|---|
| `disallowed_tools` | `[]` | Tool names that are always blocked. |
| `rate_limits` | `{}` | Per-tool sliding-window limit: `{max, window_seconds}`. |

## `prompt_cache`

`PromptCacheMiddleware` (`graph/middleware/prompt_cache.py`) does two things at the model-call boundary: (1) **delivers** the volatile knowledge/skills/hot-memory context that `KnowledgeMiddleware` produces — `create_agent` builds a static system prompt and doesn't read the `context` state key, so this is what actually gets that context to the model; (2) sets Anthropic **`cache_control`** on the stable system-prompt prefix, with the volatile context placed *after* the breakpoint so it never invalidates the cached prefix.

Caching is **attempt-by-default with fail-loud watching** (#2255): blocks are attached for every model — gateway aliases included, since the alias name says nothing about what it routes to. A provider that *rejects* `cache_control` gets one automatic retry without blocks and falls back to plain delivery for that model for the session (logged once); a provider that *silently ignores* the blocks (repeated calls with a cacheable-size prefix and zero cache activity in usage) draws a WARNING naming the model — silent full-price billing is the failure mode this exists to kill. **Context delivery happens regardless**, so the middleware is always wired.

```yaml
prompt_cache:
  enabled: true     # caching half (delivery is unconditional) — attempted on EVERY model
  ttl: "5m"         # "5m" ephemeral, or "1h" persistent (agent turns exceed 5m)
  force: false      # never auto-fall back: a provider rejection propagates
  warm:             # cache-warming heartbeat (off by default)
    enabled: false
    interval_seconds: 3300   # 55m — just under the "1h" tier
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Attach `cache_control` to the stable prefix for every model. Rejection → auto-fallback (per model, per session); silent zero-hit → a once-per-model WARNING. `false` = plain delivery only. |
| `ttl` | profile-aware | Cache tier: `5m` (ephemeral) or `1h` (persistent). Absent, it resolves by profile (#2780, ADR 0101 D7): **`1h` on fleet members and the packaged desktop app** (long-lived agents that idle past 5m between turns — a single avoided prefix re-warm covers the 1h tier's higher write price), `5m` on an interactive dev instance. Explicit config always wins. |
| `force` | `false` | Trust-the-operator mode: always attach, never auto-fall back (a rejection propagates instead of degrading silently). |
| `warm.enabled` | `false` | Run a background heartbeat (`graph/cache_warmer.py`) that periodically reproduces the cached system prefix so the **first** request after an idle gap hits a warm cache instead of a full miss. |
| `warm.interval_seconds` | `3300` | Heartbeat period. Set just under `ttl` (default 55m for the `1h` tier). |

**When to enable `warm`:** sporadic but latency-sensitive traffic on the `1h` tier — the ~1-token ping per interval is cheap relative to a cold miss on a multi-thousand-token prefix while a user waits. Leave it **off** for steady traffic (the cache stays warm on its own — warming is then pure cost) and for providers where the zero-hit warning fired (nothing to warm). It runs as its own asyncio task (started/stopped with the server), **not** through the scheduler — the scheduler fires full agent turns, the wrong primitive for a keep-alive.

## `pruning`

In-history tool-result pruning (#2782, ADR 0101 D3/D4) — the near-lossless relief step that runs **before** compaction's lossy summarize. At `at_fraction` of the model's context window (chars÷4 estimate; a fixed conservative floor when the gateway reports no window), tool results older than the newest `keep_messages` are rewritten to bounded head + omission marker + bounded tail, in **one batched pass** so the rolling history cache breakpoints (#2777) take a single miss instead of one per call. Replacement is by message id, so tool-call pairing survives; the marker says the middle is gone and to re-run the tool if needed. A pass here often keeps the 0.8 compaction valve from firing at all.

```yaml
pruning:
  enabled: true       # on by default
  at_fraction: 0.6    # prune at 60% of the window — before compaction's 0.8
  keep_messages: 20   # the newest N messages are never touched
  min_chars: 4000     # results smaller than this aren't worth a marker
```

## `compaction`

Wires langchain's `SummarizationMiddleware` to summarize old history near the context limit (enables long-horizon runs; we otherwise only cap via `max_iterations`). Opt-in.

```yaml
compaction:
  enabled: true
  trigger: "fraction:0.8"   # or "tokens:120000" / "messages:80"
  keep_messages: 20          # most-recent messages kept verbatim
  model: ""                  # blank = summarize with the main model; or a cheaper one
```

## `execute_code`

Opt-in **programmatic tool calling** (the bundled `execute_code` plugin, `plugins/execute_code/`). Adds an `execute_code` tool: the model writes one Python script that calls several tools, loops/filters/composes their results in code, and `print()`s only the final answer — collapsing a long tool-call chain into a single turn (the model reads just the stdout, not every intermediate payload).

The script runs in a **child process** with a **scrubbed environment** (only `PATH` + the bridge fds — no gateway keys / auth tokens) and a **hard timeout**. Tools are invoked back in the **parent** over an fd-based RPC bridge, so they run with the parent's credentials, audit, and trace context; the child only orchestrates. Inside the script, tools are reached via an injected `tools` object (`tools.web_search(query=...)`). The `execute_code` tool never exposes itself, so scripts can't recurse.

```yaml
execute_code:
  enabled: false           # OFF by default — runs model-authored code
  timeout: 30.0            # seconds before the child process is killed
  tools: []                # allowlist; empty = all tools except execute_code
  output_truncate: 6000    # cap on returned stdout (chars)
```

| Key | Default | What |
|---|---|---|
| `enabled` | `false` | Register the `execute_code` tool. |
| `timeout` | `30.0` | Wall-clock limit; the child is killed past it. |
| `tools` | `[]` | Tool-name allowlist exposed to scripts (empty = all but `execute_code`). |
| `output_truncate` | `6000` | Max returned stdout chars. |

> **Security:** subprocess + env-scrub + timeout is *isolation, not a true sandbox* — the child can still touch the filesystem and network as the server user. Enable only for trusted-model output or inside a hardened container (seccomp / read-only FS / network policy). Narrow `tools` to the minimum the workload needs.

## `tools`

**Deferred tools** — progressive tool disclosure for high tool counts ([ADR 0005](../adr/0005-tool-pollution-and-progressive-disclosure.md)). When enabled, only a small base set + a `search_tools` meta-tool are shown to the model each turn; the rest stay bound (callable) but their schemas are withheld until the agent calls `search_tools` to load them. This cuts the per-turn tool-schema footprint and improves selection accuracy once you routinely exceed ~15 tools.

```yaml
tools:
  disabled: []              # tool names to DROP (the operator's denylist)
  hidden: []                # tool names to REMOVE ENTIRELY — denied AND never shown in the console
  subagent_only: []         # tool names kept OFF the lead — only subagents that allowlist them get them
  deferred:
    enabled: false          # OFF by default — the full tool set is shown
    keep: []                # always-on tool names; empty = built-in base
```

| Key | Default | What |
|---|---|---|
| `disabled` | `[]` | Tool names to **drop** from the agent at graph build — covers the **fully assembled** set: core, plugin, MCP, the delegation tools, and the filesystem tools (so `disabled: [run_command]` removes shell access for this agent). Live-reloadable — in the console, **every row at Settings ▸ Capabilities ▸ Tools carries an on/off switch** that edits this list (a toggled-off tool stays listed, dimmed, so it can be re-enabled). Plugins still ADD tools on top (see [Plugins](/guides/plugins)). ([ADR 0005](../adr/0005-tool-pollution-and-progressive-disclosure.md)) |
| `hidden` | `[]` | A **hard superset of `disabled`** (#2172, [ADR 0071](../adr/0071-plugin-permissions-trust-model.md)): a hidden tool is denied at the graph like a disabled one, **and dropped from the console's tool inventory entirely** — it never renders as a toggle, so it can't be re-enabled from the UI. A setup-time trust control for restricted consoles and archetypes: the config file is the boundary, the UI is presentation. `disabled` = "off but visible"; `hidden` = "not available, and not offered". |
| `subagent_only` | `[]` | Tool names **kept off the lead agent** but still available to any subagent whose `tools` allowlist names them ([ADR 0117](../adr/0117-subagent-only-tools.md)). The lead model is never offered them and a lead call to one is blocked, so it reaches them only by delegating with `task`: the domain's tools, their schemas and the context they generate stay inside the subagent that owns it. Background subagents get them too. They are kept out of `search_tools`, execute_code's bridge and the operator MCP. `disabled` still wins. Graph build logs a name no subagent allowlists, and filesystem or late tools, which only a background subagent can reach. The console's Tools list marks them "Subagents only". |
| `deferred.enabled` | `false` | Withhold most tool schemas; expose them via `search_tools`. |
| `deferred.keep` | `[]` | Tool names always shown. Empty → built-in base (keyless core + `task`/`task_batch`/`run_workflow`/`save_workflow` + `search_tools`). `search_tools` is always kept regardless. |

Every tool remains **executable** even while deferred — `create_agent` registers all executors; deferral only trims what the model *sees* per turn. The agent loads tools by calling `search_tools("github pull request")`; matches stay available for the rest of the thread. Leave off unless you have a large catalog (e.g. a chatty MCP server) — for a handful of tools it adds a discovery hop for no benefit.

## `settings`

**Hide settings from the console** — the settings half of `tools.hidden` (#2172,
[ADR 0071](../adr/0071-plugin-permissions-trust-model.md)).

```yaml
settings:
  hidden: []   # dotted field keys ("goal.max_iterations") or whole groups ("goal", "careercoach")
```

| Key | Default | What |
|---|---|---|
| `hidden` | `[]` | Entries are dotted settings keys (`goal.max_iterations`) or **group prefixes** (`goal` — plugin groups like `careercoach` work too). A hidden setting is dropped from the schema the console renders **and refused by the settings save/reset APIs**, so it can't be seen or changed from the UI. The live **value is untouched** (hiding ≠ disabling) — this locks a setup-time decision, it doesn't turn the feature off. Like `tools.hidden`, it's meant for restricted consoles and archetype enforcement (a bundle's `config:` block can seed it at create time). |

## `telemetry`

Local per-turn cost/latency rollup ([ADR 0006](../adr/0006-observability-and-the-self-improving-flywheel.md)). One row per terminal turn leg — from either turn driver (`server/turn_telemetry.py::record_turn`) and, since #3015, one per CLI coding-agent run (tokens incl. cache, USD cost, duration, LLM/tool call counts), queryable at `/api/telemetry/summary` + `/api/telemetry/recent`.

```yaml
telemetry:
  enabled: true                 # one cheap write per turn
  db_path: /sandbox/telemetry.db
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Write a per-turn row at terminal time. `false` → no store; endpoints return `{enabled:false}`. |
| `db_path` | `/sandbox/telemetry.db` | SQLite path; the legacy `/sandbox` default resolves to `<instance_root>/telemetry.db`. |

## `tracing`

Langfuse deep tracing — the other half of [ADR 0006](../adr/0006-observability-and-the-self-improving-flywheel.md). Where `telemetry` is the SQL rollup (one row per turn), this is the trace *tree*: the tool calls, model calls and subagent spans inside a turn, plus the `trace_id` each telemetry row carries so the console can deep-link a turn to its trace. It is also what makes `a2a.trace` propagation pay off — a delegation nests under its caller's trace instead of opening its own.

Credentials resolve **environment first, config second** (#3017), and **env keys never follow a config host** (#3039): a key pair from `LANGFUSE_{PUBLIC,SECRET}_KEY` goes to `LANGFUSE_HOST`/`LANGFUSE_URL` or the default, never to `tracing.host` — config reaches an instance through more paths (snapshot import, a fork's committed YAML) than deployment env does, so it must not aim deployment credentials. The reverse is a fallback, not a block: config keys use `tracing.host`, then `LANGFUSE_HOST`, then the default, so host-in-env + keys-in-Settings keeps working. A host that loses is named on the boot line rather than dropped. A container deploy that exports `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` keeps working with no config at all, and those env vars win over anything here. The config block below is the fallback — and it is the only path that reaches a **desktop-launched fleet member** (`protoagent-server --port … --ui none`), which is spawned with no `LANGFUSE_*` in its environment. Editable in-app at **Settings ▸ Tracing** — in the Agent group rather than beside the `telemetry` rollup under Box, because these are per-agent credentials and the Box group renders only on the host console; a fleet member's window would never have shown them. The Telemetry surface carries a gear onto the same four fields.

```yaml
tracing:
  enabled: true
  host: https://cloud.langfuse.com
  # public_key / secret_key are SECRETS — the Settings UI stores them in
  # secrets.yaml, never here. Hand-editing them into this file works, but the
  # next save relocates them to secrets.yaml (like every other credential).
```

| Key | Default | What |
|---|---|---|
| `enabled` | `false` | Connect to Langfuse using the key pair below. Ignored when `LANGFUSE_{PUBLIC,SECRET}_KEY` are both in the environment — an env-configured deploy traces regardless, so this is a fallback toggle, not a kill switch. |
| `host` | `LANGFUSE_HOST`, else `http://host.docker.internal:3001` | Base URL of the Langfuse instance **for the key pair below**. Blank falls back to `LANGFUSE_HOST` / `LANGFUSE_URL`, then the default, which only resolves inside the bundled Docker compose. Env-supplied credentials use `LANGFUSE_HOST` and never this (#3039). |
| `public_key` | `""` | Langfuse public key. **Secret** → `secrets.yaml`. |
| `secret_key` | `""` | Langfuse secret key. **Secret** → `secrets.yaml`. |

Changing any of these needs a **restart** — the client is built once at boot. With tracing off, `/api/telemetry/recent` reports `tracing_enabled: false` and the console's Trace column says `off` rather than showing a blank that reads as "this turn wasn't traced". A toggle turned on without a full key pair is not silent either: the boot log says `tracing.enabled is on but the Langfuse key pair is incomplete`. Walkthrough: [Wire Langfuse + Prometheus](/guides/observability#langfuse-distributed-traces).

## `prompts`

Per-call system-prompt snapshots (#2243) — the persistence behind the console's
**View prompt** message action and the `/prompt` chat command: the EXACT prompt every
model call received (the stable prefix hash-deduped, the volatile context tail per
call), plus that call's real token usage. Instance-scoped SQLite
(`prompt-snapshots.db`), trimmed in-write — no maintenance loop. Deleting a chat purges
its snapshots. Read surface: `GET /api/prompts/{task_id}` + `GET /api/prompts/last`
(operator `/api` only; both return `{enabled:false}` when capture is off).

```yaml
prompts:
  capture: true                 # one hashed blob + a small tail per model call
  retention_days: 30
  max_calls: 5000
```

| Key | Default | What |
|---|---|---|
| `capture` | `true` | Snapshot each model call's final system prompt. `false` → nothing recorded; the viewer reports capture disabled. |
| `retention_days` | `30` | Prune snapshots older than this on each new capture (`0` = keep forever). |
| `max_calls` | `5000` | Keep at most this many snapshots, newest first (`0` = unlimited). |

Both caps trim inside the same write, so **the smaller one is the real window** — at a
few hundred model calls a day the row cap is reached in days and `retention_days` never
bites (#3019). `GET /api/prompts/last` returns a `retention` block
(`{retention_days, max_calls, calls, oldest_ts, newest_ts, effective_days, binding_cap}`)
naming which cap — if either — is currently ending the window, the caps as
**configured** against the rows actually held, so the effective window is readable
without opening the SQLite file.
`/prompt` in chat surfaces the same thing: when the row cap is what ends the window, the
"nothing captured" note says so rather than implying the session was simply never
captured.

## `filesystem`

Fenced multi-project filesystem toolset ([ADR 0007](../adr/0007-directory-aware-operator-agent.md)) — a generic primitive that gives the agent read/write/list/search + fenced command execution over a registry of project directories. It is **ON by default**, fenced to a default `workspace` dir when no explicit `projects` are set (override with `PROTOAGENT_WORKSPACE`). The capability a forked operator (e.g. "Roxy") composes into a multi-project manager — see the [operator-fork guide](../guides/operator-fork.md).

```yaml
filesystem:
  enabled: true                  # ON by default
  allow_run: true                # run_command available (ON); HITL-gated below — false = never built
  run_requires_approval: true    # each run_command pauses for operator approval
  bypass_allowed: true           # false = /bypass can't skip the approval gate
  run_auto_approve: []           # command prefixes that skip the approval prompt (see below)
  editor_command: ""             # e.g. zed / "code -g" / "cursor -g" — binds open_in_editor
  code_pane: false               # the console code pane toolset (show_code + /api/fs/file|diff) — opt-in
  editor_handoff: true           # open_in_editor also offers this chat to a new Zed agent thread
  projects:
    - { name: orbis, path: /Users/kj/dev/ORBIS, write: false }   # read-only monitor
    - { name: pixelgen, path: /Users/kj/dev/pixelgen, write: true }
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Expose the fs tools (`list_projects`/`read_file`/`list_dir`/`find_files`/`search_files`/`write_file`/`edit_file`/`delete_file`). Off → no fs tools. |
| `allow_run` | `true` | Also expose `run_command` (fenced `cwd`, but arbitrary argv — dual-use, like `execute_code`). **`false` is the per-agent kill switch**: the tool is never built, so the model can't see or call it. |
| `run_requires_approval` | `true` | Each `run_command` call pauses for HITL operator approval (A2A `input-required`). Drop to `false` to let commands run unattended. |
| `bypass_allowed` | `true` | Permit the per-tab `/bypass` chat toggle to skip the approval gate. `false` = approvals enforced regardless of caller-supplied metadata. |
| `run_auto_approve` | `[]` | Safe-command allowlist: command **prefixes** in argv terms (`git status`, `git diff`, `npx vitest run`, `mise exec -- npm test`) whose matching `run_command` calls skip the approval prompt. Matched **token by token** from the start after `shlex` splitting — `git diff` covers `git diff --stat` but not `git difftool`, and `FOO=1 git diff` matches nothing. A command containing any of `` ; & \| ` $ ( ) < > \ * ? [ ] { } ~ # ! ``, a newline/control character or non-ASCII whitespace **always asks**, even inside quotes (`git log --format='%H;x'` asks); so does any denylisted option (`--output`/`-o`, `--exec`, `--config`/`-c`, `--ext-diff`, `--upload-pack`, `--require`, `--prefix`, find's `-exec`/`-delete`, … — including abbreviations like `--out=` and bundled short flags like `-po`). A matched command runs **directly from its tokens, with no shell**, and its result starts with `(auto-approved: matches "<entry>")`; each one is logged at INFO. POSIX `/bin/sh` grammar only — `shell: powershell`/`cmd` always ask. Entries are validated when the tools build: one with a metacharacter, a `VAR=` prefix, a denylisted option, or that is too broad (`sh`, `env`, `npx`, `node`, bare `git`/`npm`, `npm run`, `mise exec --`, `uv run`, …) is **dropped with a warning**. Only consulted when approval is on and `/bypass` isn't. Empty = every command asks. See [the guide](../guides/sandboxing.md#run-auto-approve) for a starter list and the caveats. |
| `editor_command` | `""` | Your desktop editor's command line — `zed`, `code -g`, `cursor -g`. When set, binds `open_in_editor(project, path, line?)`, which pops a fenced file open in that editor **on the machine the agent runs on** ("open the router for me"). Split with shlex; the target is appended as one argument, `<abs_path>[:<line>]`. Same fence as every fs tool, works in read-only projects, launched detached (never waits). Empty = the tool is not bound — leave it unset on a headless/remote agent. Also the console's last-resort **Open in editor** for plugin views (`POST /api/fs/open-in-editor`) when its own editor link has none — only for a console on the same machine; `/api/runtime/status` reports `editor_command_configured`. **Windows:** point it at the editor's real `.exe` (quote a path with spaces), e.g. `"C:\Users\<you>\AppData\Local\Programs\Microsoft VS Code\Code.exe" -g` — a `.cmd`/`.bat` launcher (VS Code's `code` is `code.cmd`) is refused, because Windows runs it through `cmd.exe`, which would interpret characters in file names. |
| `code_pane` | `false` | The console **code pane** as an opt-in toolset ([ADR 0112](../adr/0112-console-code-pane.md)). On: the agent gets `show_code(project, path, line, end_line?, note?)` (a `code-ref` chip that opens the pane at the range), the console shows the read-only **Code** surface (File \| Diff) and offers **protoAgent** under Settings ▸ Chat ▸ Open files in, and `GET /api/fs/file` + `GET /api/fs/diff` answer. Off (default): no tool, both routes answer 404 `{code: "disabled"}`, no Code surface, and fs path links open the external editor. Needs `enabled`. Hot-reloadable; the console picks the change up from `/api/runtime/status` `code_pane.enabled` without a reload. |
| `editor_handoff` | `true` | When `open_in_editor` opens a file, also **offer the calling chat to the editor**: a new agent thread started in Zed's agent panel (through the `protoagent-acp` shim) under that project — its root, a folder inside it, or a parent folder up to 3 levels above it (never `/` or the home folder itself) — within **2 minutes** continues this conversation (same A2A context, history replayed) instead of starting fresh. The tool result says so. One-shot, latest offer wins (one pending offer per chat), in memory only. The console's **Continue in Zed** chat-tab action makes the same offer by hand. `false` = the tool only opens the file. |
| `projects` | `[]` | Managed workspaces: `{name, path, write, no_delete}`. **Empty falls back to a default `workspace` dir** (so the tools are usable out of the box). **Every path is fenced under a project root** (`..`/symlink escapes refused); `write:false` makes a project read-only; `write:true` + `no_delete:true` is read-write-no-delete (create/edit, never delete — the third Cowork mount mode); invalid paths are skipped. |

The four toggles, the auto-approve list and the **Code pane** switch are editable per agent in the console via the **Shell & filesystem** chip on **Settings ▸ Capabilities ▸ Tools** (hot-reload — a save rebuilds the graph). `tools.disabled: [run_command]` (above) is an equivalent per-tool route — in the console, that's the `run_command` row switch in the same panel's Filesystem group.

**Security:** the project roots are the **hard fence** — every tool resolves paths under a root and refuses escapes; `write_file`/`edit_file` need `write:true`; `delete_file` additionally needs `no_delete:false` and **always** pauses for approval (a permanent-delete floor the `/bypass` toggle and `run_auto_approve` can't skip); the agent's own repo is not a project unless you add it. All mutations are audited. `run_auto_approve` is a friction reducer, **not a boundary**: a listed test runner (`npm test`, `npx vitest run`) executes project code, and even `git status`/`git diff` honour repository config (`core.fsmonitor`, diff drivers, filters) — all of which an agent with `write:true` on that project can edit. List read-mostly commands, and treat an allowlisted runner in a writable project as "the agent may run code here unattended". See ADR 0007 §4 and ADR 0083 D5.

## `projects`

The managed-projects registry ([ADR 0095](../adr/0095-managed-projects-registry.md)) — the **one** place a repository the agent works on is declared: where it lives on this host, what it is on GitHub, and what the agent may do to it. Consumers *project* from it instead of re-declaring: the filesystem fence (`filesystem.projects` above), the GitHub plugin's repo picker, and the project board's repo / base branch. An explicit `filesystem.projects`, `github.repos`, or `project_board.repo` still wins, so existing config is untouched.

```yaml
projects:
  - name: my-agent                    # the identifier the fs tools address it by
    path: ~/dev/my-agent
    github: you/my-agent              # feeds the github repo picker + the board's PRs
    default_branch: main              # feeds the board's worktrees
  - name: notes
    path: ~/dev/notes
    write: false                      # read-only
  - name: theirs
    path: ~/dev/theirs
    github: them/theirs
    fs: false                         # tracked for github/board only — no filesystem reach
```

| Key | Default | What |
|---|---|---|
| `name` | — | Identifier; becomes the fs project name and the board's `project:` handle. |
| `path` | — | Absolute (or `~`) path to the checkout on this host. A missing path is skipped with a warning. |
| `github` | — | `owner/name`. Feeds the GitHub plugin's repo picker and the board's `Fixes #N` / PR target. |
| `default_branch` | `main` | The branch the board cuts worktrees from and opens PRs against. |
| `write` | `true` | A registered project is fenced **read-write** by default — set `false` for read-only. |
| `no_delete` | `false` | With `write: true`, forbid `delete_file` (create/edit, never delete). |
| `fs` | `true` | `false` keeps the entry for GitHub/board consumers only — no filesystem tools reach it. |

Two things write entries here besides you. A bundle's Configure step: a `config_inputs` `path` prompt flagged `project: true` registers the answered checkout at create time (core ≥ 0.146, #2977) — `{name: <dir name>, path, github: <owner/name from its origin remote>, write: <onboarding.write_default, i.e. false unless set>}` — and, **only when no `onboarding:` section exists and a GitHub remote was parsed**, seeds `onboarding: {enabled: true, root: <the checkout's parent>, allow: ["github.com/<owner>/<name>"]}` — exactly the typed repo, nothing wider (no remote → registered only, `onboarding` untouched). Once this list is non-empty it *is* the filesystem fence, same as a tool-driven `onboard_project` — the default writable workspace entry no longer applies. The full wording is in the [bundles guide](../guides/bundles.md). And the `onboard_project` / `register_local_project` tools themselves (when `onboarding` below allows it). `show_config(section="projects")` shows what the running agent resolved.

## `onboarding`

The consent gate for an agent **registering a repository itself** — the `onboard_project` tool (clone a repo, then register it), the `register_local_project` tool (register a directory already on disk), and the `onboard-project` skill the project-manager archetype ships. Both tools write the `projects` registry above. An agent that can add project roots can widen its own filesystem fence, so the operator declares where that is allowed: `root` bounds every registration, and `allow` bounds what may be cloned.

```yaml
onboarding:
  enabled: true
  root: ~/dev                         # clones land here; every registration must resolve UNDER it
  allow:                              # clone sources the agent may onboard (glob on host/owner/repo)
    - "github.com/protoLabsAI/*"
    - "gitlab.com/acme/*"
  write_default: false                # registered read-only unless the call asks for write
  approve_outside_root: true          # a folder OUTSIDE root (or any, with no root) → ask the operator
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | A discoverability switch, not the consent — `root`, `allow` and your answer on an approval card are. `root` and `allow` are empty by default, so a stock install clones nothing and registers a local folder only when you approve it on the card (below). Off → both tools are **absent from the toolset entirely**. |
| `root` | `""` | Clones land here, and `register_local_project` accepts only a directory that **resolves** (symlinks followed) strictly inside it. A directory outside it — or any directory while it is unset — goes to your approval card instead (unless `approve_outside_root: false`, which refuses). Only you can widen it; the agent cannot. |
| `allow` | `[]` | `fnmatch` globs (same semantics as `plugins.sources.allow`) matched against a clone source's canonical **`host/owner/repo`**, for any git host: `github.com/acme/*`, `gitlab.com/acme/*`, `git.example.com/team/*`. A bare `owner/repo` means `github.com`; an https URL, an ssh URL and the scp form `git@host:owner/repo` of the same repo all normalize to the same string (port and credentials dropped). Empty = nothing may be cloned; it does not gate `register_local_project`, which fetches nothing. |
| `write_default` | `false` | Whether a registration is fenced read-write unless the call says otherwise. |
| `approve_outside_root` | `true` | When `register_local_project` names an existing directory **outside** `root` — or any directory while `root` is unset — park the turn on an in-chat approval card for that one folder instead of refusing. `false` = refuse, as before. See below. |

**Registering a folder outside the root.** With `approve_outside_root` on (the default), an
agent asking to register a local folder outside `root` — or any local folder on a stock install
where `root` is unset — shows you an approval card — in the
console, the deck and Zed alike — instead of a refusal. The card is written by the server, not
the agent: the folder's **resolved** absolute path (symlinks followed; the path the agent typed
is shown separately when it differs), whether it is a git checkout and its `origin` remote, the
access the agent asked for, and the root it falls outside (or, with no root set, that this
agent has no default workspace and approving registers only this folder). Your choices are **Allow read-only**,
**Allow read-write** or **Deny**; your choice wins over the agent's `write`. Allowing registers
exactly that folder in `projects:` (an audit line is logged either way); `root` stays the
boundary for everything else, and nothing else is widened. A denial comes back to the agent as
a plain result, so it can offer an alternative (clone the repo into the root with
`onboard_project`).

The card is **never auto-approved**: not by `/bypass`, not by the console's "Approve & don't
ask again", not by Zed's "Allow for this session". Those skip confirmation of actions *inside*
the fence you already drew; this card moves the fence, and the change outlives the turn and the
session, so a standing "yes" would let the agent widen its own reach at will. For the same
reason each allow answer is bound to the path on the card — a plain "approve" (an older client,
or any auto-approver) registers nothing, and if the folder resolves somewhere else by the time
you answer (a swapped symlink), nothing is registered either. No git command runs in the folder
until you approve it.

Some places are refused outright, with no card: the filesystem root; your home directory and
anything containing it; `~/Library` and `~/AppData`; `~/.config`, `~/.local`, `~/.cache`,
`~/.protoagent`; any path through a credentials directory (`.ssh`, `.gnupg`, `.aws`, `.azure`,
`.kube`, `.docker`, `.password-store`, keychains, …) or with a secret-looking name; protoAgent's
own box and instance directories (and anything containing them); system directories (`/System`,
`/Library`, `/Applications`, `/usr`, `/etc`, `/bin`, `/sbin`, `/var`, `/private`, `/opt`, `/dev`,
`/proc`, `/sys`, `/boot`, `/run`, `/lib*`, `/snap`, `/nix`, `/root`; on Windows `Windows`,
`Program Files*`, `ProgramData`); the temp directories themselves (a folder *inside* `/tmp`,
`/private/tmp`, `/var/tmp` or `/var/folders` can be approved); the bare parents `/Users`, `/home`,
`/Volumes`, `/mnt`, `/media`, `/srv`; and any path containing control characters. These apply
the same whether or not `root` is set. (`onboard_project` still needs `root`: clones only ever
land under it.)

git receives the clone URL exactly as given, so the host's ssh keys and credential helpers apply; a credential embedded in an https URL is masked in everything the tool reports (git itself still stores the URL in the checkout's `.git/config`, so prefer a credential helper). Refused before git runs: local paths and `file://`, remote-helper transports such as `ext::`, and anything starting with `-`.

## `network`

How this server listens, which reverse proxies it believes, and the public console address,
if you run one. Edit them in **Settings ▸ Box ▸ Network**. `bind` and `trusted_proxies` are
host-scoped (`host-config.yaml`) and a restart applies them. `public_console_url` is per agent
(`langgraph-config.yaml`) and applies on save.

```yaml
network:
  bind: 0.0.0.0
  trusted_proxies:
    - 172.18.0.0/16   # the Docker network your proxy container is on
  public_console_url: https://console.example.com   # per agent; see below
```

| Key | Default | What |
|---|---|---|
| `bind` | `127.0.0.1` | The interface the server listens on. An explicit `--host` wins. Env fallback: `PROTOAGENT_HOST` (see [environment variables](./environment-variables.md)). |
| `trusted_proxies` | `[]` | IPs or CIDRs of reverse proxies allowed to set `X-Forwarded-For` and `X-Forwarded-Proto`. Loopback (`127.0.0.1`, `::1`) is always trusted, so a proxy on the same machine (`tailscale serve`, `cloudflared`, nginx on the host) needs nothing here. Add a proxy that reaches you from **another** address, such as a container on a Docker network or a separate box. The list goes to uvicorn (`forwarded_allow_ips`), and it decides which client the [pairing-claim throttle](../explanation/security-and-trust.md#the-pairing-claim-endpoint) counts a wrong code against: from an untrusted proxy, every client looks like the proxy and they share one budget. A header from anyone not on the list is ignored. `*` trusts every peer, which is only safe when nothing but your proxy can reach the port. Env fallback: `FORWARDED_ALLOW_IPS` (comma-separated, uvicorn's own variable). |
| `public_console_url` | `""` | Your own HTTPS origin for this agent's console, such as `https://console.example.com`, served by a proxy you run ([ADR 0121](../adr/0121-device-pairing-over-an-operator-supplied-public-url.md) D2). Set, it is offered in **Settings ▸ Devices** as **Phone without Tailscale**, so a phone can pair without Tailscale ([guide](../guides/phone-access.md#without-tailscale)). Rules, checked when the config loads (a bad value names this key and fails the load or the save): `https://` only; an origin only, with no path, query or fragment (a trailing `/` and an explicit `:443` are dropped); not loopback. The agent **refuses to start** with it set and no auth token, and `PROTOAGENT_ALLOW_OPEN=1` doesn't override that. The origin is added to `A2A_ALLOWED_ORIGINS` when that allowlist is set, and to the trusted Host names. A pairing claim through this hostname must be `https` at your proxy (via `X-Forwarded-Proto` from a trusted proxy). It is never discovered: other offered addresses stay tailnet and LAN only. Per agent, so a fleet member doesn't inherit the hub's environment value. On the [credential plane](#credential-plane-keys). Env fallback: `PROTOAGENT_PUBLIC_CONSOLE_URL`. Proxy setup: [the console on its own hostname](../guides/exposing-protoagent.md#console-hostname). |

## `egress`

Deny-by-default outbound-host allowlist ([ADR 0008](../adr/0008-sandboxing-and-openshell.md)) enforced in `fetch_url` — the tool where the model picks an arbitrary host (the in-process exfiltration / SSRF vector). Also the single source of truth the OpenShell network policy is generated from (`scripts/gen_openshell_policy.py`). Editable in the console at **Settings ▸ Box ▸ Network** (host-scoped, hot-reloads).

```yaml
egress:
  allowed_hosts:
    - api.proto-labs.ai
    - "*.github.com"      # wildcard: apex + any subdomain
```

| Key | Default | What |
|---|---|---|
| `allowed_hosts` | `[]` | Hosts `fetch_url` may reach. **Empty = permissive** (off, with a built-in SSRF guard still blocking private / loopback / cloud-metadata addresses). When set, any other host is denied. `*.host` matches subdomains + apex; case-insensitive, port-agnostic. Hot-reloads. |

When the allowlist **is** set, your configured model gateway (`model.api_base`) host is permitted **automatically** — you don't have to list it, and the connection-test / "Get models" probes for a custom base URL won't be blocked. (With an empty allowlist this auto-add is a no-op; adding one host there would flip the guard into deny-by-default for every other host.) Covers `fetch_url` only; `execute_code`/`run_command` process-level egress is fenced by running under OpenShell (see [Sandboxing & egress](../guides/sandboxing.md)).

## `security`

Opt-in CIDR allowlist for the outbound A2A destinations the agent POSTs to — push-notification **callbacks** (caller-supplied webhook URLs) and **`peer_consult`** (`PEER_<HANDLE>_URL`). Empty/unset = today's behavior: callbacks keep their default private-IP denylist (`a2a_stores`), `peer_consult` is unrestricted.

```yaml
security:
  callback_allowlist:
    - 100.64.0.0/10   # tailnet
    - 10.0.0.0/8      # private fleet
```

| Key | Default | What |
|---|---|---|
| `callback_allowlist` | `[]` | CIDRs an outbound callback / peer destination may resolve into. **Empty = off.** When set it becomes the policy: a destination is allowed iff **every** resolved IP is inside a listed range (overrides the default callback denylist, so you can permit a specific internal/tailnet range; everything else is rejected). Hot-reloads. |
| `redact_tool_output` | `true` | Scrub credential patterns from tool output **before** it reaches the model, session transcript, and checkpoints. Patterns include OpenAI keys (`sk-…`), GitHub tokens (`ghp_…`), Bearer tokens, env-var assignments (`OPENAI_API_KEY=…`), AWS access keys, Slack/Discord tokens, and any value the [external secrets manager](./configuration.md#secrets_manager) has fetched. Audit logging is always redacted regardless of this flag. Set `false` only when debugging a credential issue that requires seeing the raw output. |

## `routing`

Wires langchain's `ModelFallbackMiddleware`: on a primary-model error, retry on each fallback model (same gateway) in order. Opt-in (empty = no fallback). `aux_model` is a separate, optional cheap/fast alias for non-reasoning calls.

```yaml
routing:
  fallback_models: [claude-haiku-4-5, gpt-5]
  aux_model: ""        # cheap/fast alias for summarization, goal-verify, subagent delegation
```

| Key | Default | What |
|---|---|---|
| `fallback_models` | `[]` | Models to retry on a primary-model error, in order. Empty = no fallback. |
| `aux_model` | `""` | Single cheap/fast alias for non-reasoning calls (compaction summarizer, goal verifier, subagent delegation). Blank = everything runs on the main model; each path's own override still wins. |

### Mixing a subscription with the gateway

A native OAuth connection (`type: anthropic-oauth` / `openai-codex`, [ADR 0097](/adr/0097-native-oauth-subscription-providers)) bypasses the gateway entirely — so LiteLLM's own `fallbacks:` chain can't see those calls, and a subscription hiccup or an expired credential is otherwise a hard stop.

Any slot that takes a model name — `routing.fallback_models`, `routing.aux_model`, `compaction.model`, `goal.eval_model`, `model.favorites`, a subagent's `model` — can **name its own connection** (`<id>:<model>`, [ADR 0106](/adr/0106-provider-registry)), so slots don't all follow the lead. The ids below are the default ones; any registered [`providers`](#providers) id works the same way:

| Slot value | Routes to |
|---|---|
| `gateway:protolabs/coder` | the LiteLLM gateway |
| `anthropic-oauth:claude-sonnet-5` | your Claude subscription |
| `openai-codex:gpt-5.6-sol` | your ChatGPT subscription |
| `acp:claude` | that CLI coding agent over ACP |
| `protolabs/coder` | the gateway (shorthand — a `/` implies a gateway alias) |
| `claude-sonnet-5` | the lead provider — the legacy `model.provider` (the gateway by default) |

Hold a gateway key and both subscriptions and you can mix all of them at once — Claude for review, Codex for code, the gateway for cheap bulk work — whatever the main brain runs on. The qualified form is the one to reach for when two providers could plausibly serve the same model id.

::: tip Mixing providers mid-conversation costs reasoning continuity, not correctness
On `openai-codex`, the model's reasoning is threaded across turns as an encrypted blob that
is **sealed to the endpoint and account that minted it** — a blob replayed anywhere else is a
hard `400`. protoAgent stamps each captured item with its issuer and silently drops the ones
the current endpoint can't decrypt, so switching a chat's model mid-thread (or re-signing-in
under a different ChatGPT account) just restarts reasoning continuity from that point. The
conversation itself is unaffected.
:::

```yaml
providers:
  - id: gateway
    type: openai-compat
    base_url: https://api.proto-labs.ai/v1
  - id: anthropic-oauth
    type: anthropic-oauth
model:
  name: anthropic-oauth:claude-sonnet-5        # main brain on your Claude subscription
  provider: anthropic-oauth                    # legacy compatibility — see the note under `model`
routing:
  fallback_models: [gateway:protolabs/coder]   # degrade to the gateway if the subscription can't serve
  aux_model: gateway:protolabs/fast            # cheap calls never touch the subscription
```

A `gateway:` slot needs that connection's endpoint (and its key, if it takes one). A bare `/`-alias slot under a subscription lead routes through the legacy single-gateway fields instead, so it needs `model.api_key` or `OPENAI_API_KEY`; without one the alias is ignored with a warning, since there'd be nothing to route to. A gateway alias as the **main** `model.name` under a native provider is still an error — that's a misconfiguration, not a fallback.

#### Removing a connection that slots still name

`DELETE /api/config/providers/{pid}` **refuses (409)** while any slot still routes through the connection — dropping it under those slots would turn a qualified `pid:model` value into a bare model id sent to whatever provider remains (a wrong-provider call, not an error). `GET /api/config/providers` reports those dependencies twice per row: `in_use_by` as display strings, and `in_use` as `[{key, value, kind, clearable}]` for the Providers panel to act on (`kind` is `slot` \| `favorite` \| `subagent`; `model.name` is never `clearable`).

To remove an in-use connection, resolve its slots **in the same request** with an optional body:

```jsonc
// DELETE /api/config/providers/prod-gateway
{ "releases": {
    "model.name": "local-vllm:reasoning",   // repoint → <other_pid>:<model>
    "routing.aux_model": null,               // clear the slot
    "model.favorites": null,                 // drop only this connection's favorites
    "subagents.researcher.model": null
} }
```

Each key must be one the walk currently reports for `pid`; a repoint target must name **another** registered connection; `model.favorites` accepts only `null` (it drops just the `pid:`-prefixed favorites, keeping the rest); and `model.name` **must be repointed, not cleared** — the lead model always has to resolve. Every release is applied in memory and the references re-checked *before* anything is written: a body that leaves any reference dangling is still a 409, and the provider list plus the released slots persist in one transaction (so a failed rebuild rolls back both). The response is `{ "ok": true, "removed": pid, "released": [<keys applied>] }`. A bare `DELETE` with no body is unchanged.

## `goal`

**Goal mode** (`graph/goals/`) lets you give the agent a *testable outcome* it self-drives toward. After each terminal turn (the agent stops with a final answer), the goal's **verifier** decides whether it's met; if not, the agent is re-invoked with a continuation prompt — carrying the verifier's evidence and the running plan the agent records with the `update_goal_plan` tool — until the verifier passes, the iteration budget runs out (`exhausted`), or the goal is flagged `unachievable` (a no-progress streak, or the agent calling the `abandon_goal` tool). Unlike a pure-LLM "are we done?" check, completion is backed by a real verifier.

The machinery is wired when `enabled`, but **no goal is active until one is set** via the `/goal` control message (works over A2A / the React console / OpenAI-compat) or the `/api/goals/{session_id}` endpoints. State is persisted per session under `GOAL_PATH` → `/sandbox/goals` → `~/.protoagent/goals`.

```yaml
goal:
  enabled: true            # machinery available; no goal active until set
  max_iterations: 8        # continuation budget per goal
  no_progress_limit: 3     # consecutive unchanged progress -> unachievable
  eval_model: ""           # blank = routing.aux_model, then main model
  verify_timeout: 120      # default seconds for command/test verifiers
  max_rounds_per_turn: 50  # model rounds per goal-driven turn; 0 = unlimited
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Wire goal mode. No goal runs until set. |
| `max_iterations` | `8` | Max continuation turns before a goal is `exhausted`. |
| `no_progress_limit` | `3` | Consecutive unchanged progress checks before `unachievable`. Concrete verifiers compare reason/evidence; LLM goals compare the saved plan. |
| `eval_model` | `""` | Model for the `llm` verifier; blank uses `routing.aux_model`, then the main model. |
| `verify_timeout` | `120` | Default wall-clock seconds for `command`/`test` verifiers; their `timeout` spec overrides it. CI checks use the GitHub CLI runner's separate timeout. |
| `max_rounds_per_turn` | `50` | Model rounds per goal-driven turn. At the cap, verify then pause if unmet; the goal stays active until a new message or trigger resumes it. `0` disables this cap. The smaller of this and `model.round_hard_cap` applies. Hot-applies on save. |

Use [Goal mode](/guides/goal-mode) for creation, management, and
[verifier types](/guides/goal-mode#verifier-types). Chat `/goal` accepts LLM,
plugin, and workspace-relative data substring checks. Command, test, CI, and
data expression checks require the operator panel or API.

## `watches`

Settings for [watches](/guides/watches), which poll external conditions and can
schedule a reaction turn.

```yaml
watches:
  enabled: true
  interval: 30
  keep_terminal_h: 24
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Bind `create_watch`, `list_watches`, `update_watch`, and `clear_watch` for the agent. Disabling does not stop the poller or alter stored watches. Independent of `goal.enabled`. |
| `interval` | `30` | Global polling cadence in seconds, clamped to at least 5. A watch's `interval_s` adds a further floor. Re-read every tick. |
| `keep_terminal_h` | `24` | Hours to retain `met`/`expired` records. `0` keeps them indefinitely. Active watches never age out. |

## `knowledge`

The [Knowledge reference](/reference/knowledge#the-knobs) owns retrieval keys,
defaults, and store locations. It also covers [shared and layered stores](/reference/knowledge#sharing-knowledge-across-a-fleet-the-commons)
and [automatic delivery controls](/reference/knowledge#memory-delivery-controls-adr-0069).
Use [Tune knowledge recall](/guides/knowledge) to compare retrieval settings.

## `context`

`context.budget_pct` and `context.prior_sessions` control the injected context
budget and prior-session selection. See [Context and session digest](/reference/knowledge#context-and-session-digest)
for defaults, shedding order, and the effect of `off`.

## `memory`

`memory.max_sessions` and `memory.max_tokens` limit the prior-session digest.
See [Context and session digest](/reference/knowledge#context-and-session-digest)
for defaults, storage, and invalid values. Use [Manage memory](/guides/manage-memory)
to inspect, correct, or remove saved context.

## `skills`

Human-authored skills in the AgentSkills [`SKILL.md`](../guides/skills.md) format — a folder with YAML frontmatter (`name` + `description`) and a markdown body. Loaded from disk into an FTS5 index on boot; `KnowledgeMiddleware` lists the index (name + summary) as an always-on `<available_skills>` block and the agent loads a skill's full body on demand via `load_skill` ([progressive disclosure, ADR 0060](../adr/0060-skill-progressive-disclosure.md)).

The [Skills reference](/reference/skills#configuration-skills-block) owns this
block's field schema, storage tiers, and defaults, including the difference
between the fallback description limit and the bundled example's explicit value.

Skills load from bundled `config/skills/`, live `<config-dir>/skills/`, the console's
`<instance_root>/skills/`, and enabled plugins' skill directories. Live skills
override bundled ones by `name`. `GET /api/runtime/status` reports `skills.count`.
See the [Skills guide](../guides/skills.md) for authoring.

## `a2a`

Your fork's **A2A [agent card](agent-card.md) identity** — the advertised skills and description a caller sees. Declare them here (or contribute card skills from a plugin via `register_a2a_skill`) instead of editing `server/a2a.py`. Distinct from `skills` above: those are disk `SKILL.md` *procedural memory* retrieved at inference; these are what the *card* advertises. Omit both keys and the template ships one free-text `chat` placeholder so a fresh clone stays callable. The card `name` follows `identity.name` / `AGENT_NAME`.

```yaml
a2a:
  description: "Acme Bot — turns support tickets into triaged, drafted replies."
  skills:
    - id: triage_ticket
      name: Triage Ticket
      description: Classify a support ticket and draft a reply.
      tags: [support]
      examples: ["triage ticket #1234"]
      # Optional structured output — enforced + emitted as a typed DataPart (#476):
      # result_mime: application/vnd.protolabs.triage-v1+json
      # output_schema: { type: object, properties: { ... }, required: [ ... ] }
```

| Key | Default | What |
|---|---|---|
| `description` | template placeholder | The agent card's `description`. |
| `skills` | one `chat` placeholder | Advertised `AgentSkill`s (`id`/`name`/`description` + optional `tags`/`examples`). A skill declaring `result_mime` + `output_schema` returns schema-enforced structured output as a typed DataPart ([#476](agent-card.md)); the MIME is advertised in its `output_modes`. |
| `require_routable_url` | `false` | When `true`, refuse to boot if the card would advertise a loopback URL (e.g. `A2A_PUBLIC_URL` unset on a deployed agent → silently unreachable to remote callers). Off by default — local/desktop runs *should* advertise loopback. |

## `mcp`

Connect external [Model Context Protocol](../guides/mcp.md) servers; their tools become agent tools (namespaced `<server>__<tool>`). Off by default — adding a server is the opt-in. Built on `langchain-mcp-adapters`.

| Key | Default | What |
|---|---|---|
| `enabled` | `false` | Connect the configured servers and expose their tools. |
| `timeout_seconds` | `20` | Per-server **discovery** timeout. A slow/unreachable server is skipped, never fatal. Does not bound a tool call — that's `call_timeout_seconds`. |
| `call_timeout_seconds` | `300` | Bounds a single tool **invocation**. A trip cancels that call and returns a recoverable tool error the model can retry with narrower arguments — never a failed turn. `0` disables it. Deliberately generous: real calls do run for minutes (a filesystem search over a large tree measured ~4 min), so this is a backstop against a call that will *never* return, not a latency budget. |
| `max_result_chars` | `50000` | Bounds a single tool **result** (#2781, ADR 0101 D3). Over the cap, the result is rewritten to bounded head + omission marker + bounded tail — the marker names the true size and this knob. The one lane that previously had no size cap: an over-large result otherwise re-enters the context on every later model call for the thread's life. `0` disables it; a server entry can override with `max_result_chars`. |
| `denylist` | `[]` | Namespaced tool names to drop (e.g. `filesystem__write_file`). |
| `servers` | `[]` | List of `{name, transport, …}`. `stdio` → `command`/`args`/`env`/`cwd`; `streamable_http`/`sse` → `url`/`headers`. Per-server: `enabled: false` skips connecting it (lazy); `tools: {include: [...], exclude: [...]}` filters which of its tools bind; `call_timeout` overrides `call_timeout_seconds` for that server alone; `max_result_chars` likewise overrides the global result cap (`0` = uncapped). |

Per-server `tools.include` is an **allowlist** (only those tools bind) — the fix for a server with a large catalog flooding context; `exclude` drops from the remainder (`include` wins on conflict). The global `denylist` is the cross-server hard block. Both match the bare or namespaced tool name. See [ADR 0005](../adr/0005-tool-pollution-and-progressive-disclosure.md) on tool pollution.

Servers are discovered at startup/reload. `GET /api/runtime/status` reports `mcp.servers` and `mcp.tool_count`. See the [MCP guide](../guides/mcp.md) and `examples/mcp/echo_server.py`.

## `checkpoint`

The conversation-history checkpointer (durable chat memory across restarts) and its pruning/harvest knobs.

```yaml
checkpoint:
  db_path: /sandbox/checkpoints.db   # blank = in-memory (history lost on restart)
  keep_per_thread: 5
  max_age_days: 30
  prune_interval_hours: 6
  harvest_enabled: true
```

| Key | Default | What |
|---|---|---|
| `db_path` | `/sandbox/checkpoints.db` | SQLite path (legacy `/sandbox` default resolves to `<instance_root>/checkpoints.db`). Blank → in-memory (chat history doesn't survive a restart). |
| `keep_per_thread` | `5` | How many checkpoints to retain per conversation thread. |
| `max_age_days` | `30` | Drop checkpoints older than this. |
| `prune_interval_hours` | `6` | How often the background pruner runs. |
| `harvest_enabled` | `true` | On thread retire, harvest its history into the knowledge store before purging. |
| `vacuum` | `true` | After a prune frees rows, truncate the WAL and run `incremental_vacuum` so the file shrinks. |
| `prune_on_write` | `true` | Apply `keep_per_thread` (`background_keep` for background jobs) after **every** checkpoint write, not just on the sweep, so one long turn can't grow a session without bound ([ADR 0120](/adr/0120-checkpoint-growth-bounds-and-alerts)). It keeps the newest checkpoints a resume needs, plus any rows an in-flight subagent wrote into the session. |
| `offload_media` | `true` | Store large base64 or `data:` images in message content (screenshots, renders, pasted pictures) once per history DB in a `checkpoint_media` table, instead of inside every checkpoint row. The checkpointer's serializer does this, so any LangGraph saver given `graph.checkpoint_serde.protoagent_serde(conn)` (a stock `SqliteSaver`, LangGraph Studio, a script) reads the same messages, images included. |
| `alert_db_mb` | `5120` | A warning banner appears in the console when `checkpoints.db` plus its WAL passes this size. On a fleet hub it also appears for each local member's DB. `0` turns the alert off. |
| `alert_thread_mb` | `1024` | A warning banner names any single session whose stored history passes this size. `0` turns the alert off. |
| `vacuum_legacy_on_start` | `true` | At startup, before the DB opens, migrate an `auto_vacuum=NONE` history DB once (`PRAGMA auto_vacuum=INCREMENTAL; VACUUM;`) so its freed space goes back to the disk. Runs only when at least half the file (or 256 MB) is free space, the disk has room for the rewrite, and no other process has the DB open. It logs the size before and after. On demand, with the agent stopped: `protoagent checkpoint migrate` (`--workspace <member>` or `--db <path>` for another DB). |

To reclaim a bloated history DB, run `protoagent checkpoint compact` (with `--workspace <member>` or `--db <path>` for another DB). It is a **dry run** by default: it lists the sessions it would trim, how many bytes that frees, and the expected file size, using read-only queries that are safe while the agent runs. Add `--apply` to trim, drop unreferenced media and return the freed pages to the disk. Run `--apply` while the agent is quiet: the vacuum step rewrites the pages it moves.

## `background`

Background subagent jobs (`task(run_in_background=true)`, [ADR 0050](/adr/0050-background-subagents-reactive-notifications)) and how their results are delivered ([ADR 0070](/adr/0070-background-results-push-resume)).

```yaml
background:
  auto_resume: true
```

| Key | Default | What |
|---|---|---|
| `auto_resume` | `true` | When a background job finishes, immediately run a turn in the session that spawned it — its `<task-notification>` drains into that turn and the agent briefs the operator. `false` restores pull-only delivery (the report waits for the session's next manual turn; the ADR 0050 Activity idle-wake covers autonomous reaction instead). Never fires for canceled jobs, incognito-spawned jobs, or jobs spawned from another background turn. |

## `workflows`

Storage for [reusable workflows](/guides/workflows). Enable the bundled
`workflows` plugin through `plugins.enabled`; `workflows.enabled` is obsolete
and is not read by the configuration parser.

```yaml
workflows:
  dir: /sandbox/workflows
```

| Key | Default | What |
|---|---|---|
| `dir` | `/sandbox/workflows` | Writable recipe root. The legacy `/sandbox` default resolves to `<instance_root>/workflows`; an explicit non-`/sandbox` path is used as supplied. |

Bundled recipes load from `plugins/workflows/recipes/`; enabled plugins can
contribute more directories. Writable recipes override same-named recipes.
Saved run records live in `.runs/` beneath the writable root.

The plugin's separate `workflow_runs.max_runs` setting defaults to `200` finished
runs. Running and paused records are not pruned by that history limit.

## `plugins`

Drop-in [plugins](../guides/plugins.md) (manifest + `register()`) that contribute tools, bundled skills, FastAPI routes, background surfaces, subagents, and managed MCP servers (ADR 0018/0019). They run **in-process** with the agent's privileges, so a third-party plugin is **disabled by default** — only enable plugins you trust. Bundled plugins declare their defaults in their manifests; Discord and Google are external plugins installed separately.

| Key | Default | What |
|---|---|---|
| `enabled` | `[]` | Plugin `id`s to load. A plugin also loads if its own manifest has `enabled: true`. |
| `disabled` | `[]` | Plugin `id`s to force **OFF** even when their manifest says `enabled: true` — the way a fork drops a bundled plugin (e.g. `friction`) without deleting its directory or editing core. |
| `dir` | `""` | Override the writable plugins root (default `<instance_root>/plugins`). |
| `sources.allow` | _(absent)_ | Optional list of host/org glob strings for git-URL installs (e.g. `[github.com/yourorg/*]`). A scalar string or a non-string entry is rejected; CLI installs also fail closed when the policy cannot be read. Absent-vs-explicit-empty is semantic (#2743, same rule as `sources.official`): key **absent** = any URL allowed (gated install); an **explicit `[]`** = deny-all — the hardening stance a defense-minded admin writing an empty list means. A config carrying a literal `allow: []` from before #2743 flips from open to deny and the boot log says so loudly — remove the key to stay open. (ADR 0027.) |
| `update_policy` | `{}` | Opt-in background auto-updates, keyed by plugin `id`. Each value is `{track, when}`: a non-empty `track` **arms** the plugin (the ref comes from `plugins.lock`); `when` is `idle` (default — defer while a chat turn is/was just in flight) or `always`. A SHA-pinned plugin is never auto-updated. Empty = manual-only (the default). (#1720; see the [Plugins guide](../guides/plugins.md#keeping-plugins-current).) |
| `autoupdate_interval_hours` | `6` | Cadence of the auto-update sweep in hours; `0` disables the loop. Only plugins in `update_policy` are ever touched. |

Plugins load from two roots — bundled (`plugins/`, e.g. `hello`, `friction`, `plugin-devkit`) and writable (`<instance_root>/plugins/`, where git-URL installs land); live overrides bundled by `id`. Plugin tools that shadow a core/MCP tool are skipped. `GET /api/runtime/status` reports `plugins[]` (`id`, `enabled`, `loaded`, `tools`, `skills`, `views`, routes/surfaces/subagents counts). Plugins are installable from a git URL (`python -m server plugin install <url>`) — see [Install plugins](../guides/plugin-registry.md) — and a repo can ship tools, subagents, skills, workflows, and console views. See the [Plugins guide](../guides/plugins.md).

### Plugin-declared config sections (ADR 0019)

A plugin can **claim a top-level config section** and declare its keys/secrets/Settings in its manifest (`config_section` / `config` defaults / `secrets` / `settings`). The section is resolved (manifest defaults ⊕ YAML ⊕ secrets overlay) into `config.plugin_config["<section>"]` and surfaced as a Settings group — with **no** edit to `config.py` / `config_io.py` / `settings_schema.py`. This is where the **Discord** config now lives:

```yaml
discord:                 # claimed by plugins/discord/ — NOT a core config field
  enabled: false
  admin_ids: []
  # bot_token → secrets.yaml (plugin-declared secret)
```

A plugin section colliding with a reserved built-in (`model`, `mcp`, `plugins`, …) is ignored. Plugin secrets (e.g. `discord.bot_token`) route to `secrets.yaml` dynamically — see **Secrets** above. External plugins (e.g. a Google or Slack integration installed from its own repo) claim their own sections the same way.

## Scheduler

Scheduler **enable/disable** is YAML-controlled (`middleware.scheduler` above) so the drawer can flip it without a restart. Backend **selection and runtime knobs** (which backend, where to write the sqlite, where to publish, etc.) are env-driven so the same container image can run under either backend without a rebuild. See [Schedule future work](/guides/scheduler) for the full guide.

| Env var | Default | What |
|---|---|---|
| `SCHEDULER_DB_DIR` | `<instance_root>/scheduler` | Explicit parent-directory override; jobs use `<override>/<agent_name>/jobs.db` when set. The default private path is `<instance_root>/scheduler/agent/jobs.db`. |
| `SCHEDULER_INVOKE_URL` | `http://127.0.0.1:<active_port>` | Local backend: where to POST `message/send` when a job fires. Override only if the agent's A2A endpoint isn't on localhost. |
| `SCHEDULER_DISABLED` | unset | Runtime escape hatch — set to `1` / `true` to drop the scheduler tools entirely without editing YAML. `middleware.scheduler: false` is the canonical opt-out. |

## Plugin automatic updates

A git-URL-installed plugin is pinned in `plugins.lock` at the ref you installed
(a branch, a release tag, or an exact commit). By default updates are **manual**:
the console **Plugins** panel shows an *Update available* badge when a plugin is
behind its ref, and the **Update** button pulls the latest code + hot-reloads it
(`POST /api/plugins/{id}/update`).

To let chosen plugins update themselves in the background, opt them into an
**auto-update policy** (#1720):

```yaml
plugins:
  autoupdate_interval_hours: 6      # sweep cadence; 0 disables the loop entirely
  update_policy:
    my-plugin:
      track: main                   # arms auto-update (the ref itself comes from the lock)
      when: idle                    # idle (default) | always
    another-plugin:
      track: main
      when: always
```

Each sweep, for every plugin listed in `update_policy`, the runtime checks whether
it's behind its locked ref and — if so — pulls + hot-reloads it exactly like the
**Update** button, then emits `plugin.updated` on the [event bus](/guides/plugins#event-bus).
The gates:

- **Opt-in only.** A plugin is auto-updated only if it appears in `update_policy`
  with a non-empty `track`. Everything else stays manual.
- **Never a pinned commit.** A plugin pinned to an exact SHA is immutable and is
  skipped; a release-tag pin moves to the newest matching tag, a branch pulls its head.
- **`when: idle`** (the default) defers a plugin's update while a chat turn is — or
  was just — in flight. A reload rebuilds tools/routers, which is safe *between*
  turns but disruptive *during* one. **`when: always`** updates on the next sweep
  regardless.

The default config has an empty `update_policy`, so nothing auto-updates until you
add a plugin to it.
