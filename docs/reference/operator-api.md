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
| GET | `/api/chat/sessions/{id}` | One session + its **busy signal**: `{session_id, active, turn_count, last_updated, last_state}`. `active` is true while a turn is running on the session from any surface (console, A2A, `/api/chat`); the Zed shim polls it before sending so two turns never interleave. Unknown or deleted → 404 `{detail: {code: "not_found"}}` |
| DELETE | `/api/chat/sessions/{id}` | Delete a session (`?harvest=true` to extract memory first; `?forget=true` to remove what it already wrote to memory: its compaction archives and harvested summaries/facts; `?retire=false` clears it but keeps the id) |
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

## Files & code pane

Read-only. `browse` is the settings folder picker and deliberately reaches outside the fs
fence (names only, never contents). The other three stay inside it — the same
`registry.resolve` chokepoint `read_file` uses ([ADR 0007](../adr/0007-directory-aware-operator-agent.md)) —
and `file`/`diff` never return a secret-like file's content ([ADR 0112](../adr/0112-console-code-pane.md)).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/fs/browse` | List the server's directories for a path picker (`?path=&files=&hidden=`) |
| GET | `/api/fs/roots` | `{roots: {project: absolute root}}` — the live fs fence |
| GET | `/api/fs/file` | `?project=&path=[&start=&end=]` → `{project, path, size, line_count, start, end, truncated, language, binary, text}`. Lines are `\n`-delimited, endings preserved; capped at 2 MB / 20,000 lines / 2,000 chars per line (`truncated: true`, page with `start=end+1`). Binary → `text: null`. Errors carry `detail: {code, reason}`: `bad_path` 400 (unknown project / fence escape), `denied` 403 (secret-like name, checked before existence), `not_found` 404, `not_a_file` / `bad_range` (also a non-integer `start`/`end`) / `unreadable` 400 |
| GET | `/api/fs/diff` | `?project=` → the working tree vs `HEAD`: `{project, is_git, head, branch, files: [{path, status: M\|A\|D\|R\|?, additions, deletions, binary, denied, old_path?, reason?, too_large?}], patch, truncated}`. Hardened git (no external diff, textconv, filter, fsmonitor, hook or submodule recursion can run), scoped to the project root, untracked text files ≤ 256 KB as synthetic new-file patches, secret-like paths — and symlinks resolving outside the project or onto one — listed `denied` (with a `reason`) and content omitted, larger untracked files flagged `too_large`, patch capped at 1 MB and the file list at 5,000 entries (`truncated: true`). Not a repo → `{is_git: false, files: [], patch: ""}`; 10 s timeout → 504 |

## Editor hand-off

Continue a console chat in Zed's agent panel. Zed can't deep-link into an agent thread, so the
console (**Continue in Zed**) or `open_in_editor` *offers* the session and the `protoagent-acp`
shim *claims* it when the operator starts a thread. In memory, per instance; one offer per
project root (latest wins) and one per chat (a new offer for a chat replaces its older ones
under every root; a claim removes them all), 120 s TTL, one-shot.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/editor/handoff` | Body `{session_id, project?, path?, line?, title?}` → `{id, expires_at, root}`. `project` resolves through the fs fence to its root; omitted → `root: null`, which matches any folder. Unknown session → 404 `not_found`; a project outside the fence → 400 `unknown_project`; a `path` escaping it → 400 `bad_path` |
| POST | `/api/editor/handoff/claim` | Body `{cwd}` → 200 `{session_id, project, path, line, title}` (and the offer is removed) or 204. Matches when `cwd` is the root, inside it, or a **parent** of it at most 3 levels up; the newest unexpired match wins. A filesystem/volume root (`/`) never matches, and the home directory itself never matches a project offer (only a project-less one) |

## Fleet & agents

| Method | Path | Purpose |
|---|---|---|
| GET · POST | `/api/fleet` | List / create workspace agents |
| PATCH · DELETE | `/api/fleet/{name}` | Rename / remove an agent |
| POST | `/api/fleet/{name}/{start,stop,activate}` · `/api/fleet/down` | Lifecycle control |
| GET | `/api/fleet/discover` | Discover agents (LAN mDNS + tailnet) |
| POST · DELETE | `/api/fleet/remotes[/{ident}]` | Register / remove a remote member |
| GET | `/api/archetypes` | Starter agent types (catalog + installed bundles) |
| GET | `/api/archetypes/{id}/preview` | Peek a bundle archetype's members/MCP/secrets before install |

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

## Diagnostics

Member-local, read-only reads for inspecting a fleet member without shell access (#3168). Served on **every** member, so the hub reaches a local peer and a registered remote alike via `/agents/{slug}/api/diagnostics/...`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/diagnostics/logs?lines=N` | Bounded tail of this member's log ring. `lines` is clamped (1–1000, default 200) rather than rejected; the response carries `note` when it was adjusted. |
| GET | `/api/diagnostics/tasks/{task_id}` | One exact A2A task: `state`, `status_message`, `history`, `artifacts`, `accumulated_text`, `context_id`, `last_updated`. |

**Diagnostics output is sensitive operator data** — logs and task rows can carry prompts, user content, and tool arguments. Both endpoints are operator-tier (the ADR 0066 federation credential is denied `/api` outright), read-only, bounded in history/artifact/text size, and scrubbed by the shared credential redactor before returning.

Responses degrade rather than 500: an unknown task is `404`, a member with no task store is `503`, and a malformed store row returns `200` with the unparseable columns named in `malformed[]`. A **stopped or unreachable member** is the proxy's case and answers `409`/`502`/`504` from `/agents/{slug}/*`. Truncation is always reported in `truncated[]` — a partial history is never presented as a complete one.

The log source is an in-process ring buffer sized by `LOG_BUFFER_LINES`, not `agent.log`; see [environment variables](environment-variables.md#logging) for why.
