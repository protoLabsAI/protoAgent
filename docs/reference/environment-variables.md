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

## Deployment / UI tier (ADR 0010)

| Variable | Default | What |
|---|---|---|
| `PROTOAGENT_UI` | `full` | UI deployment tier (or `--ui`): `full` (Gradio + React console + API/A2A), `console` (console + API/A2A, no Gradio), `none` (API + A2A + `/metrics` only — the lean headless stack). The Docker image defaults to `none`. |
| `PROTOAGENT_HEADLESS` | (unset) | **Deprecated** alias for `PROTOAGENT_UI=console` (or `--headless`). |
| `PROTOAGENT_HEADLESS_SETUP` | (unset) | Set `1`/`true` to auto-complete setup from a validated config even outside the `none` tier (no wizard). The `none` tier implies this. |

Setup without the wizard: `python server.py --setup` validates the live config (`model.api_base` set + key resolvable via `secrets.yaml`/`OPENAI_API_KEY`) and writes `.setup-complete`, then exits. In the `none` tier the server auto-completes the same way on boot, or **fails fast** if the config is invalid. Readiness is exposed at `GET /healthz` (503 until the graph compiles).

## Authentication — A2A bearer token

| Variable | Default | What |
|---|---|---|
| `A2A_AUTH_TOKEN` | (unset — open mode) | Required bearer token for all A2A routes (POST `/a2a`, `message/send`, `tasks/*`, SSE streaming). When set, requests without `Authorization: Bearer <token>` get 401. Token comparison uses `hmac.compare_digest` (constant-time). |

When unset, the handler logs a WARNING at startup (`"A2A auth token not configured — endpoint is open"`) and accepts all traffic — appropriate for local development, not production. When set, the agent card advertises `securitySchemes.bearer` so A2A consumers know to present credentials.

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

## UI

| Variable | Default | What |
|---|---|---|
| `GRADIO_SERVER_NAME` | `0.0.0.0` | Bind address for the Gradio UI. |
| `GRADIO_SERVER_PORT` | `7870` | Port for the Gradio UI. The A2A handler + metrics + agent card are all served on the same port. |

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

The template deliberately doesn't read `GITHUB_TOKEN`, `DISCORD_BOT_TOKEN`, or any tool-specific credentials. Those belong in your fork's tools, not the shared runtime.
