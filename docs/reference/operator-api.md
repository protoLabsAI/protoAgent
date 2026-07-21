# Operator REST API

The console drives the backend over a REST control-plane under `/api/*` (defined in
`operator_api/`). This is the **operator** surface — managing one agent and its host.
For talking *to* the agent as a client, use the [A2A endpoints](/reference/a2a-endpoints)
(`/a2a`) or the OpenAI-compatible `/v1` surface instead.

All `/api/*` routes are gated by the same bearer auth as the rest of the server (set via
`A2A_AUTH_TOKEN` / the configured token); the console attaches it automatically. This page
is a map — `operator_api/*.py` is the source of truth for exact request/response shapes.

## Runtime & health

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness probe |
| GET | `/api/runtime/status` | Setup state, model, enabled middleware, knowledge/scheduler/skills counts |

## Chat & sessions

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chat` | Run a non-streaming chat turn (the streaming path is A2A `/a2a`) |
| DELETE | `/api/chat/sessions/{id}` | Delete a session (`?harvest=` to extract memory first) |
| GET | `/api/chat/commands` | Slash-command inventory (workflows / subagents / skills) |
| POST | `/api/chat/sessions/{id}/steer` | Enqueue a mid-turn [steering](/explanation/steering) message |
| GET | `/api/chat/sessions/{id}/steer` | Peek pending steers |
| DELETE | `/api/chat/sessions/{id}/steer/{msg_id}` | Cancel a queued steer |
| GET | `/api/chat/sessions/{id}/delegations` | List running subagent delegations |
| POST | `/api/chat/sessions/{id}/delegations/{del_id}/cancel` | Cancel one delegation (lead continues) |

## Goals (goal mode)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/goals` | List goals across sessions |
| GET | `/api/goals/{session_id}` | One goal's detail — status + its durable plan artifact (`plan`, the `.plan.md` the agent maintains via `update_goal_plan`, ADR 0079) |
| POST | `/api/goals` | Set a goal. Optional completion-contract fields (ADR 0073) + `kick` (default `true`; the console panel sends `false` and drives the goal from a dedicated chat tab instead of a headless turn) |
| POST | `/api/goals/{session_id}/rearm` | Re-arm: extend an active goal's iteration budget (`add_iterations`), or reactivate a terminal one and kick a fresh drive turn |
| POST | `/api/goals/{session_id}/resume` | Kick a headless continuation for an active goal (used when a chat tab driving it is closed but the goal is kept running) |
| DELETE | `/api/goals/{session_id}` | Clear (stop) a goal. `?close_tasks=true` also closes the goal's session-scoped task backlog (ADR 0079) |

## Subagents & tools

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/subagents` | Registered subagents (allowlists, max turns) |
| POST | `/api/subagents/run` | Run one subagent manually |
| POST | `/api/subagents/batch` | Run several subagents concurrently |
| GET | `/api/tools` | Wired tools (core / plugin / MCP) |
| GET | `/api/acp-agents` | Detected ACP coding agents |

## Background jobs & scheduler

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/background` | Background subagent jobs |
| GET | `/api/background/{job_id}` | One job's full row by id (full result text; ADR 0070) |
| POST | `/api/background/{job_id}/cancel` · `/api/background/clear` | Cancel one / clear finished |
| DELETE | `/api/background/{job_id}` | Remove a job row |
| GET · POST | `/api/scheduler/jobs` | List / create scheduled jobs |
| DELETE | `/api/scheduler/jobs/{job_id}` | Delete a scheduled job |

## Knowledge & skills

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/knowledge/search` | Browse/search the knowledge store |
| POST | `/api/knowledge/ingest` | [Ingest](/guides/ingestion) a file / URL / text |
| POST | `/api/knowledge/attach` | Attach a chat upload (tiered inline-vs-index) |
| POST · PUT · DELETE | `/api/knowledge/chunks[/{id}]` | Add / edit / delete a chunk |
| POST | `/api/knowledge/delete-by-source` · `/api/knowledge/restore-by-source` | Bulk soft-delete / restore every chunk from one ingest (reversible, grace-swept) |
| GET | `/api/playbooks` · `/api/playbooks/{id}` | List / fetch skills ("playbooks") |
| POST · PUT · DELETE | `/api/playbooks[/{id}]` | Create / edit / delete a skill |
| POST | `/api/playbooks/{id}/promote` | Promote a private skill into the commons |

## Memory inspector

The audit surface for the memory delivery layer
([ADR 0069](../adr/0069-memory-delivery-layer.md) D7): the persisted session
summaries behind the `<prior_sessions>` digest, the hot-memory chunks (of which
the newest ride each turn's injection window), and the per-turn injection
record.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/memory/sessions` | List session summaries (digest fields: id, timestamp, surface, topic, message count, size, plus `in_digest`: whether the session is in the current `<prior_sessions>` injection window) |
| GET · DELETE | `/api/memory/sessions/{session_id}` | Full rendered summary (what `recall_session` returns) / delete one |
| GET | `/api/memory/hot` | List hot-memory chunks (`domain="hot"`); each row carries `injecting`: whether the chunk is in the current per-turn injection window (omitted on backends without the id-attributed reader) |
| PUT · DELETE | `/api/memory/hot/{chunk_id}` | Edit (revision stays `hot`) / delete a hot chunk |
| GET | `/api/memory/injections` | Per-model-call injection records ([ADR 0069](../adr/0069-memory-delivery-layer.md) D6), newest first: which digest sessions / hot chunk ids / RAG chunk ids entered each turn, at what approximate token cost. `?session_id=` filters to one session; `?limit=` clamps to 1–500 (default 50) |

## Activity, inbox & events

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/activity` | Provenance activity feed |
| GET · POST | `/api/inbox` | Read / add inbox items |
| POST | `/api/inbox/{item_id}/deliver` | Deliver an inbox item to the agent |
| GET | `/api/events` | Server-sent event stream (console live updates) |
| POST | `/api/events/publish` | Publish an event to the bus |
| GET · POST · PATCH · DELETE | `/api/tasks/...` | Tasks issue store (status, init, issues CRUD, close) |

## Config, setup & settings

| Method | Path | Purpose |
|---|---|---|
| GET · POST | `/api/config` | Read / write `langgraph-config.yaml` (+ SOUL) |
| GET | `/api/config/setup-status` | Wizard state |
| POST | `/api/config/setup` · `/api/config/reset-setup` | Complete / reset the setup wizard |
| GET | `/api/config/presets/{name}` | A SOUL/archetype preset |
| POST | `/api/config/models` · `/api/config/test-model` | List gateway models / test the connection |
| GET | `/api/settings/schema` | Settings UI schema |
| POST | `/api/settings` · `/api/settings/reset` | Apply / reset settings |
| GET | `/api/operations` | The ops-layer catalog — every operation (name, read/write, summary); mirrors `protoagent operations` (ADR 0075 D2) |

## Fleet & agents

| Method | Path | Purpose |
|---|---|---|
| GET · POST | `/api/fleet` | List / create workspace agents |
| PATCH · DELETE | `/api/fleet/{name}` | Rename / remove an agent |
| POST | `/api/fleet/{name}/{start,stop,activate}` · `/api/fleet/down` | Lifecycle control |
| GET | `/api/fleet/discover` | Discover agents (LAN mDNS + tailnet) |
| POST · DELETE | `/api/fleet/remotes[/{ident}]` | Register / remove a remote member |
| GET | `/api/archetypes` | Starter agent types (bundles + Basic) |

## Plugins & MCP

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/plugins/installed` · `/api/plugins/catalog` · `/api/plugins/updates` | Installed / host catalog / available updates |
| POST | `/api/plugins/install` · `/api/plugins/sync` | Install from git URL / re-sync from lock |
| POST | `/api/plugins/{id}/enabled` · `/api/plugins/{id}/update` | Enable-disable / update one |
| DELETE | `/api/plugins/{id}` | Uninstall |
| POST | `/api/mcp/servers` · `/api/mcp/servers/import` | Add / import an MCP server |
| DELETE | `/api/mcp/servers/{name}` | Remove an MCP server |
| GET | `/api/mcp/catalog` · `/api/mcp/exposed` | Curated server catalog / operator-MCP tools this instance exposes (effective allowlist + profile) |

## Telemetry & theme

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/telemetry/{summary,recent,export,insights}` | Cost/usage telemetry |
| GET · PUT · DELETE | `/api/theme` | Read / set / clear the saved theme |
