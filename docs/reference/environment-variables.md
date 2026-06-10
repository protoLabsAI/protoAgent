# Environment variables

Every env var the template reads at runtime.

## Required

| Variable | What |
|---|---|
| `OPENAI_API_KEY` | LiteLLM gateway master key (or direct provider key if not using a gateway). Read by `graph/llm.py`. |

## Identity

| Variable | Default | What |
|---|---|---|
| `AGENT_NAME` | `protoagent` | Short slug. Used as the Prometheus metric prefix, Langfuse trace tag, and in log labels. Should match what you used when forking. |
| `<AGENT_NAME>_API_KEY` | (unset — no auth) | Expected value of the `X-API-Key` header if you want to require auth on `/a2a` and `/v1/*`. Uppercased, non-alphanumeric → underscore. e.g. `MY_AGENT_API_KEY`. |

## Paths & overrides

| Variable | Default | What |
|---|---|---|
| `PROTOAGENT_CONFIG_DIR` | `<repo>/config` | Writable config root — live `langgraph-config.yaml`, `secrets.yaml`, `.setup-complete`, and the live `skills/` + `plugins/` dirs. The desktop sidecar points this at the per-user app-data dir. |
| `PROTOAGENT_WORKSPACE` | (a `workspace` dir) | Overrides the default project root for the on-by-default fenced filesystem toolset. |
| `PROTOAGENT_MODEL` | (unset) | Overrides `model.name` on every config load — used by `evals/sweep.py` to run one agent against many models without editing YAML. |
| `PROTOAGENT_INSTANCE` | (unset) | Opt-in data-scoping key (ADR 0004): namespaces the knowledge/notes/beads/checkpoint stores so several agents share a backend without colliding. Seeded from `instance.id` in config. |

## Deployment / UI tier (ADR 0010)

| Variable | Default | What |
|---|---|---|
| `PROTOAGENT_UI` | `full` | UI deployment tier (or `--ui`): `full` (Gradio + React console + API/A2A), `console` (console + API/A2A, no Gradio), `none` (API + A2A + `/metrics` only — the lean headless stack). The Docker image defaults to `none`. |
| `PROTOAGENT_HOST` | `127.0.0.1` | Bind address (or `--host`). **Defaults to loopback** so a local/desktop run isn't exposed on all interfaces — the operator/console API (`/api/*`, `/api/chat`, `/v1/*`) is otherwise reachable by anything that can hit the port. The container entrypoint + deploy manifests set `0.0.0.0` because their boundary is the published port + network policy, not the in-container bind. Binding non-loopback **without** an A2A auth token **refuses to start** unless `PROTOAGENT_ALLOW_OPEN=1`. |
| `PROTOAGENT_ALLOW_OPEN` | (unset) | Set `1` to allow a **non-loopback bind with no auth token** — the boot gate otherwise refuses it, because the open operator API includes plugin install+enable (code execution) and config rewrite. Only opt in when a network boundary fences the port (the bundled compose publishes to `127.0.0.1` only and sets this; a k8s NetworkPolicy is the other intended case). The startup log still carries a security WARNING. |
| `PROTOAGENT_HEADLESS` | (unset) | **Deprecated** alias for `PROTOAGENT_UI=console` (or `--headless`). |
| `PROTOAGENT_HEADLESS_SETUP` | (unset) | Set `1`/`true` to auto-complete setup from a validated config even outside the `none` tier (no wizard). The `none` tier implies this. |

Setup without the wizard: `python -m server --setup` validates the live config (`model.api_base` set + key resolvable via `secrets.yaml`/`OPENAI_API_KEY`) and writes `.setup-complete`, then exits. In the `none` tier the server auto-completes the same way on boot, or **fails fast** if the config is invalid. Readiness is exposed at `GET /healthz` (503 until the graph compiles).

## Authentication — A2A bearer token

| Variable | Default | What |
|---|---|---|
| `A2A_AUTH_TOKEN` | (unset — open mode) | Required bearer token for the A2A routes **and** the operator/console + OpenAI-compat APIs — `/a2a`, `/api/*`, `/api/chat`, `/v1/*`. When set, requests without `Authorization: Bearer <token>` get 401. Constant-time comparison (`hmac.compare_digest`). |

When unset, startup logs a WARNING (`"A2A auth token not configured — endpoint is open"`) and accepts all traffic — fine for local dev (and safe behind the loopback-default bind, see `PROTOAGENT_HOST`), not for an exposed deployment. **A non-loopback bind with no token refuses to start** unless `PROTOAGENT_ALLOW_OPEN=1` (see above). When set, the agent card advertises `securitySchemes.bearer`.

**Scope.** The guard covers everything that can drive the agent: `/a2a`, the operator API (`/api/*` — run subagents, rewrite config/SOUL, schedule jobs), `/api/chat`, and `/v1/*`. **Public (never guarded):** `/healthz`, `/.well-known/agent-card.json`, `/metrics`, the static console at `/app`, and the read-only `/api/events` SSE stream (browsers' `EventSource` can't send an `Authorization` header; it exposes only activity/inbox events, no action).

**Console.** When a token is set, the React console reads it from `localStorage["protoagent.authToken"]` and sends it as a bearer on every API + A2A call. Set it once in the browser (`localStorage.setItem("protoagent.authToken", "<token>")`) for a token-protected deployment; local/desktop runs (no token) need nothing.

## A2A agent-card endpoint

| Variable | Default | What |
|---|---|---|
| `A2A_PUBLIC_URL` | (unset — `http://127.0.0.1:<bound-port>`) | The externally-reachable base URL advertised in the agent card's `supportedInterfaces[].url` (where peers send `message/send`). **Set this for any deployed agent** — behind a proxy / in a container the bound port isn't the address clients use. The `/a2a` JSON-RPC suffix is appended automatically (e.g. `A2A_PUBLIC_URL=https://gina.example.com` → card url `https://gina.example.com/a2a`). Unset, it falls back to the actually-bound loopback port (correct for local + the dynamic-port desktop sidecar, where the caller is on the same host). |

This is independent of the legacy `<AGENT_NAME>_API_KEY` header-based scheme (X-API-Key) documented above. You can enable one, both, or neither; bearer is the preferred mechanism going forward.

## Memory

Session memory is enabled by default. See [architecture § Session memory](/explanation/architecture#session-memory) for the full rationale.

| Variable | Default | What |
|---|---|---|
| `MEMORY_PATH` | `/sandbox/memory/` | Directory where `MemoryMiddleware` writes JSON session summaries and where `KnowledgeMiddleware.load_memory()` reads them. Writes are atomic (temp file + rename). |
| `PROTOAGENT_DISABLE_MEMORY` | (unset) | Set to `1` (or any non-empty value) to suppress disk persistence without changing `langgraph-config.yaml`. Loading still occurs if summaries exist from prior runs. |

To persist memory across container restarts, mount a volume at whatever `MEMORY_PATH` resolves to. Without a volume the directory is ephemeral.

## Knowledge store

The bundled `KnowledgeStore` (sqlite + FTS5) is enabled by default. See [Configuration § knowledge](/reference/configuration#knowledge) for the full guide.

| Variable | Default | What |
|---|---|---|
| `KNOWLEDGE_DB_PATH` | (unset — uses YAML `knowledge.db_path`) | Runtime override for the sqlite path. Falls back to `~/.protoagent/knowledge/agent.db` when the resolved path is unwritable (e.g. running locally without `/sandbox`). |

To opt out entirely, set `middleware.knowledge: false` in YAML. The memory tools (`memory_ingest`, `memory_recall`, etc.) are dropped from the agent loop when the store is disabled.

## Notes, beads & goals (agent-global working stores)

The agent's notebook, task board, and goals are **agent-global** — one
persistent, instance-scoped store each, shared by the agent's tools and the
operator console. They are *not* per-project (there's no `.automaker/notes/` or
`.beads/` inside project directories); `operator.allowed_dirs` is purely the
filesystem fence for file/shell tools, unrelated to these stores. Each falls
back from a non-writable `/sandbox` to `~/.protoagent/…` for local dev and is
instance-scoped via `PROTOAGENT_INSTANCE`.

| Variable | Default | What |
|---|---|---|
| `NOTES_PATH` | `/sandbox/notes/workspace.json` | The console Notes panel workspace + the `notes_*` tools. |
| `BEADS_DB_PATH` | `/sandbox/beads/issues.db` | The in-process beads issue store (the `beads_*` tools + the console Beads panel). |
| `GOAL_PATH` | `/sandbox/goals` | Directory of per-session goal JSON files (goal mode). |

## Audit log

| Variable | Default | What |
|---|---|---|
| `AUDIT_PATH` | `/sandbox/audit/audit.jsonl` | Directory + filename of the JSONL audit log written by `AuditMiddleware`. Read by `evals/verify.py` for side-effect assertions. |

## Scheduler

The bundled scheduler is enabled by default. See [Schedule future work](/guides/scheduler) and [Configuration § scheduler](/reference/configuration#scheduler) for the full guide. **Backend selection** is env-driven; **enable/disable** lives in YAML (`middleware.scheduler`) so the drawer can toggle without a restart.

| Variable | Default | What |
|---|---|---|
| `SCHEDULER_BACKEND` | `local` | Set to `workstacean` to **opt in** to the remote `WorkstaceanScheduler` (also requires the `WORKSTACEAN_*` vars below). Any other value / unset → the bundled `LocalScheduler`. |
| `WORKSTACEAN_API_BASE` | (unset) | Workstacean base URL. Used only when `SCHEDULER_BACKEND=workstacean`; on its own it no longer switches the backend. |
| `WORKSTACEAN_API_KEY` | (unset) | Auth token sent as `X-API-Key` to Workstacean's `/publish`. Required (with the base) when opting in to Workstacean. |
| `WORKSTACEAN_TOPIC_PREFIX` | `cron.<agent_name>` | Override the bus topic the adapter fires on, when your Workstacean install uses a different convention. |
| `SCHEDULER_DB_DIR` | `/sandbox/scheduler` | Local backend: parent directory for `<agent_name>/jobs.db`. Falls back to `~/.protoagent/scheduler/<agent_name>/jobs.db` when unwritable. |
| `SCHEDULER_INVOKE_URL` | `http://127.0.0.1:<active_port>` | Local backend: where to POST `message/send` when a job fires. Override only if the agent's A2A endpoint isn't on localhost. |
| `SCHEDULER_FIRE_TIMEOUT_S` | `600` | Local backend: how long a fire waits for the turn (`message/send` blocks until the turn is terminal). Must exceed a real turn — too low false-fails long turns. Fires run off the poll loop, so this doesn't stall the cadence. |
| `SCHEDULER_DISABLED` | (unset) | Runtime escape hatch — set to `1` / `true` to drop the scheduler tools entirely without editing YAML. `middleware.scheduler: false` is the canonical opt-out. |

> **protoLabs operators**: the fleet's Workstacean lives on the `ava` node. `WORKSTACEAN_API_KEY` is in the org's secrets manager under `secret-management → workstacean`.

## Tracing (optional)

| Variable | What |
|---|---|
| `LANGFUSE_PUBLIC_KEY` | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | Langfuse project secret key |
| `LANGFUSE_HOST` | Langfuse host URL (e.g. `https://langfuse.company.com`). Falls back to `LANGFUSE_URL`, then `http://host.docker.internal:3001`. |

If both keys are unset, tracing is disabled and every helper in `tracing.py` becomes a no-op.

## Logging

| Variable | Default | What |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Python logging level. Valid: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

The template explicitly calls `logging.basicConfig(level=INFO)` — without this, Python's default WARNING would hide `logger.info(...)` lines like "webhook delivered", making A2A issues invisible in container logs.

## Streaming / origin verification

| Variable | Default | What |
|---|---|---|
| `A2A_ALLOWED_ORIGINS` | (unset — allow all, with WARNING) | Comma-separated list of allowed `Origin` header values for SSE and WebSocket streaming endpoints (`/a2a` streaming methods, `/message:stream`, `/tasks/{id}:subscribe`). Example: `https://app.example.com,https://admin.example.com`. Set to `*` to explicitly disable origin verification without the WARNING log. Origin values are compared case-insensitively. |

When unset, all origins are accepted but a WARNING is logged at startup. When set, requests whose `Origin` header does not match any entry receive a `403 Forbidden` response. A missing `Origin` header is treated as an empty string and will be rejected when verification is enabled.

## Push notifications / SSRF guard

| Variable | Default | What |
|---|---|---|
| `PUSH_NOTIFICATION_ALLOWED_HOSTS` | (empty) | Comma-separated hostnames that bypass the private-IP check when accepting webhook URLs. Example: `workstacean,automaker-server`. |
| `PUSH_NOTIFICATION_ALLOWED_CIDRS` | (empty) | Comma-separated CIDR blocks explicitly allowed. Example: `10.0.0.0/8,172.16.0.0/12`. |

Without these set, the handler rejects webhook URLs that resolve to private / loopback / link-local IPs — defends against SSRF where a client registers `http://169.254.169.254/...` or `http://10.0.0.1/...` as a callback.

## Server bind

The server binds host `0.0.0.0`; the port is set by the `--port` CLI flag
(default `7870`) — `uvicorn.run(app, host="0.0.0.0", port=args.port)`. The A2A
handler, REST API, metrics, and agent card are all served on that one port.
(There is no `GRADIO_SERVER_NAME` / `GRADIO_SERVER_PORT` env — those are not read.)

## Plugin env fallbacks (Discord / Google)

The bundled Discord and Google plugins prefer in-app config (Settings / wizard),
but read env as a Docker/headless fallback:

| Variable | What |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token for the `discord` plugin's gateway (fallback for `discord.bot_token`). |
| `DISCORD_ADMIN_IDS` | Comma-separated Discord user IDs allowed to DM the bot (fallback for `discord.admin_ids`). |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth client for the `google` plugin's managed MCP server. |
| `GOOGLE_TOKEN_PATH` | Where the cached OAuth token lives (set by the server to the per-user config dir). |
| `GOOGLE_TZ` | IANA timezone for "today" day bounds (fallback for `google.tz`). |

## Peer federation (A2A peer-consult tools)

Register peer agents so this agent can consult them via the `peer_list` / `peer_consult` tools (added to the toolset only when at least one peer is set). See [`tools/peer_tools.py`](https://github.com/protoLabsAI/protoAgent/blob/main/tools/peer_tools.py).

| Variable | What |
|---|---|
| `PEER_<HANDLE>_URL` | Base URL of a peer agent (its `/a2a` endpoint is derived). `<HANDLE>` becomes the peer name (e.g. `PEER_ALICE_URL` → peer `alice`). |
| `PEER_<HANDLE>_TOKEN` | Optional bearer token sent to that peer if it requires auth. |

## Release pipeline (shared `release-tools` Action)

These are **CI secrets**, not env vars the template reads at runtime. The
Discord-release step of `release.yml` delegates to the shared
[`protoLabsAI/release-tools`](https://github.com/protoLabsAI/release-tools)
Action, which reads them from the job env.

| Variable | What |
|---|---|
| `GATEWAY_API_KEY` | Bearer token for the protoLabs LLM gateway; the Action rewrites raw commits into themed notes. |
| `DISCORD_RELEASE_WEBHOOK` | Discord channel webhook. If unset (`post-discord: false`), notes generate but aren't posted. |

## Not set by the template

The core runtime stays credential-light, but some bundled tools/plugins do read
their own env: the GitHub read tools authenticate via `GITHUB_TOKEN` / `GH_TOKEN`
(or `gh`'s ambient login), and the Discord/Google plugins read the fallbacks
above. Any *other* tool-specific credentials belong in your fork's tools/plugins,
not the shared runtime.
