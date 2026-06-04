# Changelog

All notable changes to protoAgent are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Add your entries under [Unreleased]** in your PR. When a release is cut,
> `prepare-release.yml` rolls them into a dated, versioned section via
> `scripts/changelog.py`. See [Releasing](docs/guides/releasing.md).

## [Unreleased]

### Changed
- **Fork & re-sync ergonomics — customize via config/plugins/env, not core
  edits.** A fork-extensibility audit found the biggest re-sync tax was the fork
  guide telling forks to `sed s/protoagent/<name>/` (~120 files diverge → every
  upstream merge conflicts) for a purely cosmetic internal rename — the
  user-facing name is already `identity.name`-driven. Quick wins:
  - **`.gitattributes`: `CHANGELOG.md merge=union`** — the changelog no longer
    conflicts on a fork merge / upstream cherry-pick (both sides' entries coexist).
  - **Tool denylist** — drop named core tools via config (`tools.disabled`,
    live-reloadable) instead of editing `tools/lg_tools.py::get_all_tools()`.
    "Keep what you want, drop the rest, add your own (plugin)" is now fully
    config + plugin driven.
  - **Release pipeline gates on the `RELEASE_ENABLED` repo variable** (not a
    `github.repository == 'protoLabsAI/protoAgent'` literal), so forks enable
    releases without editing `prepare-release.yml` / `release.yml`.
  - **Fork guide + `TEMPLATE.md` rewritten** to set the name in config + SOUL.md,
    keep the internal `protoagent` identifier, and use the repo variable.

## [0.12.0] - 2026-06-04

### Added
- **Connect Google (Gmail + Calendar) from the app — no files, no CLI (ADR 0017).**
  The Google MCP surface (Slice 2) needed a `credentials.json`, a CLI consent run,
  and a hand-edited `mcp.servers` — unreachable from the desktop app, so the agent
  had no calendar/mail. Now: a `google` config section (`client_id` / `client_secret`
  → secrets.yaml / `tz`), a **"Connect Google"** button in Settings + an OAuth-client
  step in the wizard that runs the consent flow (`POST /api/config/google/connect`
  opens your browser, caches a refreshable token in the per-user config dir), and a
  status probe (`GET /api/config/google/status` → connected account email). When
  enabled + connected the google MCP server is **auto-wired** (no `mcp.servers`
  editing) and **frozen-aware** (the bundled binary re-invokes itself, `--mcp-google`,
  since it has no `python`); the headless subprocess is load-only so it never pops a
  browser. Env/`credentials.json` remain a Docker fallback.
- **Connect Discord from the app — no env vars, no file editing (ADR 0016).**
  The Discord surface (ADR 0015) was env-only (`DISCORD_BOT_TOKEN`), started once
  at boot — invisible to the desktop app (no shell to export into; the frozen
  sidecar can't read a repo `.env`, so it connected as whatever bot was in the
  ambient env). Now Discord is configured in-app: a `discord` config section
  (`enabled` / `bot_token` → secrets.yaml / `admin_ids`), a **"Connect Discord"**
  step in the setup wizard and a **Discord section in System → Settings**, each
  with a **"Test connection"** button (a real `GET /users/@me` identity probe via
  `POST /api/config/test-discord` — shows the bot's name, catches a bad token in
  the UI). The gateway reads the config (env vars remain a Docker fallback) and
  **reconnects live on save** — no restart. Both surfaces link to a docs
  walkthrough for creating the bot + enabling the Message Content intent.
- **Setup validates the model connection before completing — no more silently
  broken agents.** The wizard accepted any API key (the models-list probe passes
  for keys that can't actually complete), so a bad/blank key only surfaced as a
  cryptic failed chat turn with no UI signal. Now: a new `validate_model_connection`
  runs a real 1-token completion (the same auth path as chat), enforced
  **server-side in `finish_setup`** — setup can't complete if the model can't
  respond, and the gateway's own message is returned to the wizard (e.g. "expected
  to start with 'sk-'"); **"Test connection"** buttons in the wizard *and* Settings
  (`POST /api/config/test-model`, offloaded so it never freezes the loop); and a
  terminal `TASK_STATE_FAILED` chat turn now renders as an errored message with an
  actionable hint (check your API key in Settings) instead of a silent "no
  response". Everything fixable in the UI.
- **White-label brand name (driven by `identity.name`).** The console topbar +
  window/tab title now follow the configured agent name (Settings → Identity),
  defaulting to `protoAgent` — a fork sets its name once and the whole UI follows,
  no hardcoded rebrand.
- **Cold-start boot gate for the desktop app.** First launch unpacks the frozen
  PyInstaller sidecar and compiles the LangGraph agent (~30s); until it answered,
  the webview flashed WKWebView's opaque "Load failed" then snapped to the setup
  wizard. A full-screen gate (`BootGate`, adapted from ORBIS's `BootStatus`) now
  holds "Starting <agent>…" over the app until the **engine is ready** — it gates
  on `graph_loaded` (not just "runtime reachable"), so it stays down while the
  setup wizard is due and re-engages for the post-setup graph compile. The runtime
  probe polls until the graph is live; an escape-hatch ("Continue anyway", after a
  grace period) means a graph that never compiles can't trap the operator, and a
  "Retry" affordance covers the engine never coming up. (Copy is name-driven.)

### Fixed
- **Config reload no longer freezes the server (#497).** `_reload_langgraph_agent`
  (graph recompile + MCP/plugin builds) ran **synchronously on the event loop**
  from the finish-setup / settings / model-change routes, so the whole server
  stopped serving for the rebuild's duration (~30s on the frozen desktop sidecar —
  every concurrent poller got a connection refusal). The reload is now **offloaded
  to a worker thread** (`asyncio.to_thread`) at those routes. The follow-up
  scheduler / Discord restart still runs **on** the loop: a new
  `_run_on_server_loop` helper marshals it onto the captured `_main_loop` via
  `run_coroutine_threadsafe` when called from the worker thread — avoiding the trap
  where the old `get_running_loop()` path silently dropped the scheduler start
  (killing the briefing). Verified: the status endpoint stays responsive
  throughout a reload, and toggling the scheduler off→on over the offloaded route
  correctly stops + restarts it.
- **Desktop webview connects to the sidecar (was "Load failed").** Two desktop
  bugs: (1) macOS WKWebView's App Transport Security blocks plain
  `http://127.0.0.1:<port>` loopback loads by default, silently failing every
  API/chat request — added `NSAllowsLocalNetworking` to the bundle `Info.plist`.
  (2) The dynamic-free-port → `window.__PROTOAGENT_API_BASE__` injection handoff
  was unreliable across Tauri v2 webview contexts (page fell back to a dead port);
  the sidecar is now pinned to the fixed fallback port (`7870`), and the client
  also reads `?__apiPort=` off the URL as a more reliable channel.
- **"Load failed" no longer sticks after finishing setup.** The setup-finish (and
  model-change) path compiles the graph inline on the event loop, freezing the
  sidecar for ~30s — concurrent pollers got connection refusals and the error
  strip (only cleared by a user action) lingered long after recovery. The strip
  now auto-clears when the engine reports ready (`graph_loaded` flips true), and
  the boot gate holds over the compile window. (Inline compile is the root cause —
  offloading it is tracked in #497.)
- **Console chat fixed for A2A 1.0 (was a never-resolving spinner).** The React
  console's `streamChat` still spoke A2A **0.3** (`message/stream` with
  `parts:[{kind:'text'}]`), but the server moved to A2A 1.0 (a2a-sdk) — which
  returns `-32601 Method not found` (HTTP 200), so the SSE reader waited forever.
  Updated to 1.0: `SendStreamingMessage`, `role:'ROLE_USER'`, member-discriminated
  `parts:[{text}]` + `messageId`/`contextId`, `A2A-Version: 1.0` header, and frame
  parsing for the 1.0 `task`/`statusUpdate`/`artifactUpdate` shapes (0.3 kept as
  fallback). Turn-complete = SSE stream close. Also fixes the brand logo path
  (hardcoded `/app/…` 404s in the desktop bundle → `import.meta.env.BASE_URL`).
- **Desktop chat renders the agent's reply (was a silent "no response").** The
  console reads the A2A turn over SSE via `response.body.getReader()`, but
  WKWebView (the desktop shell) doesn't reliably expose a readable fetch stream
  (`response.body` can be null, or the reader reports `done` with no chunks).
  `consumeSse` now clones the response up front and **falls back to a buffered
  read** when streaming yields nothing — the turn always renders (streaming is
  kept wherever the browser supports it).
- **Beads no longer requires a `project_path` for an unconfigured agent.** The
  in-process (agent-global) beads store is now ensured before route registration,
  so first launch (pre-setup) no longer binds the CLI fallback that raises
  `project_path is required` and breaks the console's Beads panel during setup.

## [0.11.0] - 2026-06-03

### Added
- **Discord long-window context (ADR 0015, slice 4 — completes #489).** Every
  Discord exchange is logged to a small SQLite turn store
  (`surfaces/discord/turn_log.py`, separate from the knowledge DB,
  instance-scoped, `DISCORD_LOG_PATH` to override). When a conversation has gone
  cold (continuity window expired) or the process restarted, the next message is
  **warmed** with the last few turns for that `(channel, user)` — prepended as a
  `<recent_conversation>` envelope (`context.py`) — restoring continuity across
  timeouts/restarts. Best-effort: a store-init failure just disables warming.
  (The recent-turns query tie-breaks by insertion id so same-millisecond bursts
  stay deterministic.)
- **Discord return-address delivery (ADR 0015, slice 3).** When the operator DMs
  the agent, the gateway records that DM channel as a **return address**; reactive
  Activity-thread output (scheduler-fired reminders, inbox `now` items, scheduled
  briefings) is then forwarded to the operator's Discord DM — so "remind me in 30
  minutes" actually arrives. A bus subscriber forwards `activity.message` to the
  captured channel; live Discord replies use per-conversation contexts (not the
  Activity thread), so there's no double-post. Capture is DM-only, idempotent,
  best-effort, and instance-scoped (`DISCORD_RETURN_ADDRESS_PATH` to override).
  Opt-in by usage — no DM, no address, nothing forwarded.
- **Inbound Discord gateway (ADR 0015, slice 2).** A native, opt-in listener
  (`surfaces/discord/`) — DMs + channel @-mentions reach the agent, replies post
  back. Raw Discord Gateway/REST v10 over `httpx` + `websockets` (both already
  core); **off unless `DISCORD_BOT_TOKEN` is set**. A Discord DM is
  conversational, so it invokes the agent as a **chat surface** with a
  per-conversation `session_id` (the LangGraph thread key) rather than the single
  `system:activity` inbox thread — preserving per-DM continuity — and publishes a
  `discord.message` bus event for console visibility. Ported the proven
  `-deprecated-gina` UX: burst debounce, conversation continuity, slow-response
  reactions (👀→✅ only when slow), auto-threading, admin allowlist
  (`DISCORD_ADMIN_IDS`). The agent invoker is injected, keeping the surface
  decoupled + tested. Long-window context + return-address delivery are
  follow-up slices. New guide: [Discord surface](docs/guides/discord.md).
- **Outbound Discord tools (ADR 0015, slice 1).** `discord_send` / `discord_read`
  / `discord_react` — the stateless REST half of the optional Discord surface.
  Raw Discord REST v10 over `httpx` (no `discord.py`). **Off by default:**
  registered only when `DISCORD_BOT_TOKEN` is set (`get_all_tools` gates on
  `discord_configured()`), so non-Discord forks aren't cluttered; a direct call
  with no token degrades to a readable error. `discord_send` auto-splits long
  messages at 2000 chars, `discord_read` clamps to Discord's 1–100, 429s surface
  the `retry_after`. The persistent inbound gateway (the native half) is a
  separate follow-up slice. Ported from `-deprecated-gina`, template-neutralized.

### Docs
- **ADR 0015 — optional native Discord surface.** Decision record for shipping
  Discord as an opt-in template surface (off unless `DISCORD_BOT_TOKEN` set): a
  native inbound Gateway-v10 listener routed through the ADR-0003 reactive inbox
  (burst debounce, conversation continuity, slow-response reactions,
  auto-threading, admin allowlist, return-address identity capture) + stateless
  outbound REST tools. Ports the proven `-deprecated-gina` patterns to the whole
  fleet; the inbound gateway is native (not MCP — MCP can't host a persistent
  stateful connection). Design only; implementation to follow.
- **Internal dev-docs area (`docs/dev/`).** A committed, team-shared home for
  engineering working-context that isn't user-facing docs or a durable ADR:
  `docs/dev/handoffs/` (dated session handoffs) + `docs/dev/notes/` (engineering
  logs / investigations). Excluded from the published VitePress site via
  `srcExclude: ["dev/**"]` (build verified — it doesn't render or ship to the
  site). `docs/dev/README.md` documents the convention and how it relates to
  ADRs, the gitignored local `HANDOFF.md`, and agent memory. Seeded with the
  v0.10.0 handoff and a roxy upstream-sync playbook.
- **Fix stale release instructions.** `docs/guides/releasing.md` + the
  `prepare-release.yml` header/PR-body/comments said the release was cut by
  *dispatching* `release.yml` (and implied Prepare Release auto-merges +
  auto-tags). Both are wrong since the 2026-06-02 no-auto-merge/tag policy:
  Prepare Release only opens the bump PR; a human merges it and **pushes the
  tag**, which is what triggers `release.yml` (`on: push: tags`). Dispatching it
  by hand afterward is redundant and 422s on the duplicate release. The release
  PR body now prints the exact `git tag … && git push` to run.

## [0.10.0] - 2026-06-02

### Added
- **Structured-skill executor finalizer (#476).** Completes the protoAgent side
  of schema-enforced skill outputs. When a turn carries a `skillHint` for a
  skill that declares an `output_schema`, the `ProtoAgentExecutor` runs a
  forced-tool-call finalizer (`graph/structured_skill.py`:
  `create_llm(...).bind_tools([submit_skill_tool(id, schema)], tool_choice=…)`
  → `validate_skill_args` → one repair → `emit_skill_result`) and appends the
  validated object as a typed DataPart alongside the text (degrades to text-only
  on failure). Uses the shared `protolabs_a2a` v0.2.0 helpers (LLM-free wire
  layer); enforcement is runtime-local per ADR-0006. Mirrors jon's live-proven
  reference.
- **Structured-skill declaration scaffolding (#476, protoAgent side).** A skill
  spec (`_SKILL_SPECS`) may declare an `output_schema` (JSON Schema) +
  `result_mime`; `_agent_skills()` then advertises the MIME in that skill's
  card `output_modes` (the A2A-native way), and `structured_skill_schema(id)`
  hands the schema to the executor's forthcoming forced-tool-call finalizer.
  The schema lives in the skill config (not the card — `AgentSkill` has no
  schema field). No schema ⇒ free text (unchanged). The forced-tool-call
  enforcement + `emit_skill_result` DataPart land once the shared
  `protolabs_a2a` helper exists; this is the non-blocking declaration/card half.

### Fixed
- **A2A restart reconciliation restored — interrupted tasks fail instead of silently vanishing (#486).**
  The #443 migration to the `a2a-sdk` `DatabaseTaskStore` dropped the bespoke
  store's boot-time reconciliation, so a task left `submitted`/`working` when the
  process stopped lingered as fake-active (its LangGraph runner is dead) until
  the 24h TTL *deleted* it — never surfacing a terminal state to pollers or push
  consumers. `initialize_a2a_stores` now runs `reconcile_interrupted_tasks`
  **before** the TTL sweep: a dialect-agnostic JSON-path `UPDATE` (the SDK itself
  filters on `status['state']`) transitions `submitted`/`working` rows to
  `failed` with an "interrupted by restart" message. `input_required`/
  `auth_required` pauses are left alone — their checkpoint survives and can
  resume. Observed on a Roxy instance (a task stuck in `submitted`); fixes the
  fork too.
- **A2A auth: caller bearer token is authoritative + origin guard is browser-only (#482).**
  Two `a2a_auth.py` correctness bugs (found via CodeRabbit on protoPen's port,
  fixed there in protoPen#145). (1) `configure()` collapsed `bearer_token` with
  the env fallback (`bearer_token or A2A_AUTH_TOKEN`), so an apiKey-only agent
  passing `""` would silently enable bearer auth from a stray env var the card
  never advertises — now only `None` (unspecified) falls back; an explicit `""`
  means bearer-off. (2) The origin allowlist rejected requests with **no**
  `Origin` header, blocking server-to-server callers (the hub, the scheduler
  loopback) — `Origin` is browser-only, so the guard now fires only when an
  `Origin` is actually present. protoAgent's install site maps its `""` default
  to `None` so the documented `A2A_AUTH_TOKEN` env path is preserved (no
  regression). New `tests/test_a2a_auth.py` pins both.
- **A2A request-level metadata was being dropped (trace + skill dispatch).**
  `_extract_caller_trace` read only `context.message.metadata`, missing
  `SendMessageRequest`-level `context.metadata` — where clients (the hub) put
  `a2a.trace` and `skillHint`. New `_request_metadata()` merges request-level
  (preferred) over message-level, fixing Langfuse cross-trace propagation and
  enabling the structured-skill dispatch. Found via jon's reference; fleet-wide
  correctness win.
- **Scheduled jobs fire again on A2A 1.0 (#477).** `LocalScheduler._fire`'s
  loopback POST to the agent's own `/a2a` was still 0.3-shaped, so the a2a-sdk
  1.1 handler rejected every scheduled fire (`-32009 VERSION_NOT_SUPPORTED`,
  then `Method not found`). Now sends the 1.0 wire shape: `A2A-Version: 1.0`
  header, method `SendMessage`, `role: ROLE_USER`, `parts: [{text}]`, with
  `contextId` + scheduler `metadata` on the message. Regression test
  `test_fire_emits_a2a_1_0_wire_shape` locks the shape (existing tests only
  covered scheduling logic and missed it). Fleet-wide — same fix as protoPen #144.
- **A2A agent card advertises a reachable interface URL.** The card's
  `supportedInterfaces[].url` was built from `f"{agent_name()}:7870"` — i.e. the
  *agent name* as the hostname plus a hardcoded port (`http://Gina:7870/a2a`),
  unreachable for any peer and wrong for the dynamic-port desktop sidecar. It's
  now `_a2a_card_url()`: an explicit **`A2A_PUBLIC_URL`** (set this for deployed
  agents — the real external base) or, unset, the actually-bound loopback port
  (`http://127.0.0.1:<port>/a2a`, correct for local/desktop).

### Changed
- **Runtime surface + shell runtime read migrated — ADR 0013 console-wide
  migration complete.** System → Runtime extracted into `RuntimePanel`
  (`useSuspenseQuery` for runtime + subagents). The **App shell** now reads
  runtime via a non-suspense `useQuery` (topbar health light + SetupWizard +
  project default) — the retry doubles as the desktop sidecar boot-probe, so the
  shell never blanks during startup. Retires App's `runtime`/`subagents`/
  `status` state, `refreshRuntime`/`refreshAll`, and the hand-rolled boot-probe
  loop. Every console data surface (goals, beads, workflows, telemetry,
  settings, inbox, schedule, run, runtime) is now on TanStack Query + Suspense +
  ErrorBoundary; only the live/edit surfaces (Notes, Activity-Thread, Chat) stay
  intentionally imperative.
- **Run surface migrated to TanStack Query (ADR 0013).** Studio → Run extracted
  from `App` into `RunPanel`: the subagent registry is a `useSuspenseQuery`, the
  single/batch launch is a `useMutation`. Loading/errors via `<Suspense>` +
  `<ErrorBoundary>`. Retires the Run form state + handlers from `App` (the
  shell-level `runtime` read is the remaining ADR 0013 item).
- **Schedule surface migrated to TanStack Query (ADR 0013).** Activity →
  Schedule (extracted from `App` into `SchedulePanel`) reads jobs via
  `useSuspenseQuery` and adds/cancels via `useMutation` (invalidating the list);
  loading/errors via `<Suspense>` + `<ErrorBoundary>`. Retires the schedule
  state + handlers + refresh-on-tab effect from `App`.
- **Inbox panel migrated to TanStack Query (ADR 0013).** Activity → Inbox reads
  via `useSuspenseQuery`, invalidates on the live `inbox.item` event, and
  dismisses via a `useMutation` (optimistic hide held above the Suspense
  boundary so a delivered item stays gone). Loading/errors via `<Suspense>` +
  `<ErrorBoundary>`; drops the `useEffect`/`onError` plumbing. (Activity →
  Thread stays imperative — it's a live message stream with a streaming send,
  like Chat/Notes.)
- **Settings surface migrated to TanStack Query (ADR 0013).** System → Settings
  reads the schema via `useSuspenseQuery` and saves via `useMutation` (which
  invalidates the schema so hot-reloaded values reload); save status/errors show
  inline. Loading/errors via `<Suspense>` + `<ErrorBoundary>`; drops the
  `useEffect`/`onError` plumbing.
- **Telemetry surface migrated to TanStack Query (ADR 0013).** System →
  Telemetry reads the summary + recent turns + insights via a single
  `useSuspenseQuery` (`telemetryQuery`), refreshes via `refetch`, and renders
  loading/errors through `<Suspense>` + `<ErrorBoundary>` — dropping its
  `useEffect`/`onError` plumbing.
- **Workflows surface migrated to TanStack Query (ADR 0013).** The Studio →
  Workflows surface now reads the recipe list + subagent registry via
  `useSuspenseQuery`, runs/deletes via `useMutation` (invalidating the list),
  and renders loading/errors through `<Suspense>` + a contained
  `<ErrorBoundary>` — dropping its `useEffect` fetches + the `onError` global
  banner. Shared `workflowsQuery`/`subagentsQuery` added.
- **Beads panel migrated to TanStack Query (ADR 0013).** The console's Beads
  surface is now a self-contained `BeadsPanel` — the issue list is a
  `useSuspenseQuery` (refetching while mounted), and create/start/close/reopen/
  delete are `useMutation`s that invalidate it; loading is a `<Suspense>`
  fallback and errors a contained `<ErrorBoundary>` retry card. Drops the
  App-level beads state/handlers + the vestigial init flow (the in-process store
  is always ready). Beads helpers moved to `app/beads.ts`. Completes the right
  panel on the query layer (Notes stays imperative for its edit state).

## [0.9.0] - 2026-06-02

### Changed
- **`protolabs_a2a` now consumed as a published git-dep, not vendored.** Dropped
  the vendored `protolabs_a2a/` copy (added by #453) and pinned the public
  package instead — `protolabs-a2a @ git+https://github.com/protoLabsAI/protolabs-a2a.git@v0.1.0`
  in `requirements-core.txt`, next to `a2a-sdk`. Single source of truth, no
  drift. The repo is public, so the Docker build needs no clone auth. Imports
  stay `import protolabs_a2a` (the installed package exposes the same module).
  Behavioral parity verified (byte-for-byte with the deleted copy) and the full
  test suite stays green.

### Added
- **HITL form/approval cards survive the A2A 1.0 migration.** On the
  `feature/a2a-1.0-protolabs-a2a` branch the `ProtoAgentExecutor` now emits a
  protoAgent-local `hitl-v1` DataPart (full `request_user_input` form /
  `run_command` approval payload) on the `input-required` frame, plus a
  human-readable text fallback — so the console renders the form / Approve-Deny
  card instead of a stringified blob. `_interrupt_payload` passes `approval`
  shapes through (not just `form`), and the console's part reader is now A2A-1.0
  aware (matches `metadata.mimeType`, reads `content.value`/flattened `data`,
  no longer requires the dropped 0.3 `kind:"data"`) — which also restores
  tool-call-v1 card rendering. `protolabs_a2a` stays the four fleet extensions.
- **A2A 1.0 migration shipped (ADR 0014, #453).** Deleted the ~2,059-LOC
  hand-rolled `a2a_handler.py` and adopted the official **`a2a-sdk` 1.1** +
  a vendored **`protolabs_a2a/`** conventions layer (the four fleet extensions —
  cost/confidence/worldstate-delta/tool-call — plus the 1.0 card builder, auth,
  and member-discriminated parts, byte-for-byte with the hub's `@protolabs/a2a`).
  `ProtoAgentExecutor` bridges the LangGraph stream onto the SDK; durable SQLite
  task/push stores (24h TTL) with an SSRF guard on push callbacks; bearer/
  X-API-Key/origin auth; card at `/.well-known/agent-card.json`. A protoAgent-
  local `hitl-v1` DataPart keeps `request_user_input` forms + `run_command`
  approval cards rendering in the console. **Merging ≠ deploying** — the
  0.3→1.0 cutover is a coordinated publish/deploy-time step (the hub +
  roxy/ORBIS/pwnDeck), not gated on this merge.
- **Console data layer: TanStack Query + Suspense + ErrorBoundary (ADR 0013).**
  The operator console adopts `@tanstack/react-query` (suspense mode) for its
  reads — loading is a `<Suspense>` fallback, failures are caught by a contained
  `<ErrorBoundary>` with a Retry button, mutations invalidate query keys, and
  live surfaces use `refetchInterval` instead of hand-rolled polls. Replaces the
  per-surface `useEffect` + busy-flag + `try/catch → global banner` plumbing.
  This PR lands the foundation (`QueryClient` at the app root, a reusable
  `ErrorBoundary` + `PanelError`/`PanelSkeleton`, `lib/queries.ts`) and migrates
  the **Goals** sidebar panel as the reference implementation. Remaining
  surfaces (beads, studio, system, activity) follow in later PRs; **Notes stays
  imperative** (it owns edit/undo/autosave state) but is wrapped in the boundary.

### Changed
- **Goals moved into the right sidebar (Notes · Beads · Goals).** Goals were a
  Studio tab; in practice a goal is *agent state* the operator watches and
  clears, like the notebook and task board — so it now sits with the agent's
  persistent working memory in the right panel (set with `/goal` in chat, as
  before). Studio is now **Workflows · Run**. The right panel also dropped its
  per-project selector + manual refresh button (notes/beads/goals are
  agent-global and self-refresh). See [ADR 0009](docs/adr/0009-studio-control-stack.md).
- **Notes are now agent-global, like beads.** The notes workspace is a single
  persistent, instance-scoped store (`$NOTES_PATH`, default
  `/sandbox/notes/workspace.json`) that the `notes_*` tools and the console
  Notes panel share — no longer per-project (`.automaker/notes/` inside project
  dirs is gone). Scattering the agent's notebook across whatever directory was
  "the project" was confusing; the agent has one notebook now. The `notes_*`
  tools and the notes/beads APIs drop their `project_path` argument (still
  accepted-and-ignored on the HTTP layer for back-compat). The console's
  right-panel **project selector is removed**: `operator.allowed_dirs` is purely
  the filesystem security fence for file/shell tools, unrelated to notes/beads.

### Added
- **Workflow builder in the console (Sprint C).** The Workflows surface gains a
  **＋ New workflow** builder — name + inputs + steps (id, subagent picker,
  prompt, `depends_on` checkboxes) + output — that saves via `POST /api/workflows`
  (validated) and is immediately runnable; a Delete action removes a recipe.
  Authoring workflows is no longer YAML-file-only. **Completes the workflow-builder.**
- **Workflow authoring API (Sprint C).** `POST /api/workflows` validates a recipe
  (against the live subagent registry + DAG checks via `validate_recipe`) and
  saves it to the writable workflows dir (immediately runnable); `DELETE
  /api/workflows/{name}` removes it. Backs the upcoming console workflow-builder.
- **Console Beads panel + API now use the in-process store (Sprint B).** The
  operator beads endpoints go through a `_BeadsStoreAdapter` to the same
  instance-scoped `BeadsStore` the agent uses — the agent and console share one
  board, no `br` CLI / per-project `.beads/`. `project_path` is accepted but
  ignored; the `br`-backed service stays as a fork fallback. **Completes the
  beads-in-process work** (store + agent tools + console).
- **Beads agent tools (Sprint B).** The lead agent gets `beads_create` /
  `beads_list` / `beads_update` / `beads_close` over the in-process store — its
  planning/task surface (the todo replacement). Booted instance-scoped in
  `server.py` and threaded through `create_agent_graph(beads_store=…)`.
- **In-process beads store (Sprint B).** A server-owned SQLite issue tracker
  (`beads/store.py`, instance-scoped) — create/list/update/close/delete with the
  beads issue shape — replacing the file-based `br` CLI. Foundation for the beads
  agent tools + the console panel rewire (next slices).
- **`request_user_input` HITL form tool (Sprint A, server side).** Generalizes
  `ask_human` from a free-text question to a **JSON-schema form** (multi-step =
  wizard): the agent calls `request_user_input(title, steps, description?)`, the
  turn pauses via the existing LangGraph `interrupt()` → A2A `input-required`, and
  the submitted form object is returned. The interrupt→`input_required` payload
  now passes richer shapes through (`{kind:"form", …}` alongside `{question}`) so
  the console can render a form vs a prompt. The input-required A2A status
  frame now carries the payload as a `hitl-v1` **DataPart** (alongside the text),
  so any client can render the form/approval, not just read the question.
- **HITL forms render in the console + resume (Sprint A).** A paused
  (input-required) turn surfaces its `hitl-v1` payload; the chat renders a
  JSON-schema form (`request_user_input`) or a prompt (`ask_human`) above the
  composer, and submitting resumes the turn on the same session.
- **Desktop notification for HITL when hidden (Sprint A).** When a turn pauses
  for input and the window isn't focused (the menu-bar-only desktop, or a
  backgrounded tab), the console fires a native notification — via the Web
  Notification API, bridged on desktop by `tauri-plugin-notification`
  (capability `notification:default`).
- **Shell (`run_command`) is now ON by default, behind HITL approval (Sprint A).**
  `filesystem.allow_run` defaults true, but each command pauses for the operator
  to **Approve / Deny** (`filesystem.run_requires_approval`, default on) — surfaced
  as a `kind:"approval"` HITL request the console renders with the command shown
  (and the A.3 desktop notification when hidden). Completes the "shell
  on-behind-approval" posture (ADR 0007 update); a fork can drop the gate inside a
  hardened container / trusted autonomous run.
- **protoLabs.studio launch splash + console footer links.** A brand bumper
  (`IntroSplash`) shows the protoLabs.studio mark for ~2.5s on launch, then hands
  off to the app via the View Transitions API (clean cross-fade; plain unmount
  where unsupported). The console's bottom utility bar gains icon-only **Docs**
  and **GitHub** links on the left.
- **`evals/sweep.py --repeat N`** — best-of-N model comparison. Runs the suite N
  times per model against the same booted agent (isolating model-sampling
  variance from boot variance) and prints a per-case `passes/N` table, scoring
  each model on the cases that passed the **majority** of runs. Surfaces
  structural gaps (e.g. a fast model that consistently won't call a tool) vs.
  one-off flakes that still clear the majority.

### Changed
- **Fenced filesystem is now ON by default (ADR 0007 update).** A fresh agent
  gets `read_file`/`write_file`/`edit_file`/`list_dir`/`search_files`/`find_files`
  fenced to a default **workspace** dir (`paths.workspace_dir` —
  `PROTOAGENT_WORKSPACE` env, else `/sandbox/workspace` or `~/.protoagent/workspace`,
  instance-scoped) when no `filesystem.projects` are configured — a capable,
  safe first run (informed by benchmarking OpenClaw/Hermes, which both ship FS
  on, + the "anticlimactic first run" UX complaint). The two **unsandboxed**
  power tools stay opt-in: `run_command` (`filesystem.allow_run`) and
  `execute_code` are fenced-cwd-but-arbitrary-argv/code as the server user, so
  they remain off until gated behind HITL approval or run in the hardened
  container.
- **Desktop: invisible title bar + macOS bundle hardening (production prep).**
  The window uses an overlay/hidden title bar on macOS (`titleBarStyle: Overlay`
  + `hiddenTitle`) — no chrome, native traffic lights float over the content;
  the console insets its topbar for the lights and acts as the drag region
  (`.is-tauri-mac`). The macOS bundle now sets `hardenedRuntime`, an explicit
  `entitlements.plist` (network client/server + WKWebView JIT only) and
  `Info.plist` (copyright), and `minimumSystemVersion: 13.0` — the config
  prerequisites for signing/notarization (the signing itself still needs certs).
- **Desktop is now a menu-bar app with the protoLabs robot tray icon.** The
  Tauri shell uses the robot mark at the proper menu-bar size (44×44, template /
  system-tinted — `icons/tray-robot.png`) instead of the squished default app
  icon, and runs **menu-bar-only** (macOS Accessory activation policy → no dock
  icon). Closing the window hides the UI while the app + sidecar keep running in
  the menu bar; reopen via the tray icon or `⌘⇧P`, and the tray's **Quit** is the
  real exit. (protoAgent owns its own menu-bar presence — the Orbis-dropdown
  consolidation was dropped.)
- **Desktop sidecar now picks a free port + runs the `console` UI tier.** The
  Tauri shell (`apps/desktop`) probes a free port instead of hardcoding 7870
  (so it coexists with any agent already on 7870, and is the base for running
  several agents at once), spawns the bundled server with `--ui console`
  (replacing the deprecated `--headless` alias), and injects the chosen base URL
  as `window.__PROTOAGENT_API_BASE__` before page load — the React console reads
  it (`localStorage["protoagent.apiBase"]` still overrides). The "main" window is
  now created in `src/lib.rs` (so the init script can run pre-load) rather than
  declared in `tauri.conf.json`.
- Retired the `protolabs/agent` gateway alias from docs, eval examples, and test
  fixtures (use `protolabs/smart` / `protolabs/reasoning`). The default model is
  already `protolabs/reasoning`; this just clears the dead alias from examples.

### Fixed
- **Desktop window wasn't draggable + external links didn't open under the
  invisible title bar.** Two parts: (1) the Tauri capability didn't grant the
  commands they invoke — `data-tauri-drag-region` → `startDragging()` and the
  Docs/GitHub links → `shell.open` — so both silently failed
  (`window.start_dragging not allowed`, `shell.open not allowed`); granted
  `core:window:allow-start-dragging` + `shell:allow-open` (and corrected the
  stale `--headless` sidecar arg scope to `--ui console`). (2) The topbar is the
  drag region, with the brand **inset** right of the native traffic lights —
  **macOS build only** (the browser has no traffic lights, so no inset there).
  Plus a little more bottom padding under the utility-bar icons.
- **Frozen desktop: console project APIs hit a nonexistent path** — the operator
  console's default project root was `__file__`'s dir, which in a PyInstaller
  onefile is the ephemeral `_MEIxxxx` extraction dir, so notes/beads failed with
  "project_path does not exist". It now resolves a stable dir when frozen
  (`PROTOAGENT_PROJECT_DIR` override → the desktop's `PROTOAGENT_CONFIG_DIR` →
  home); a source checkout still uses the repo root. The console also self-heals
  a stale persisted project path (e.g. a `_MEI` dir saved by an earlier run):
  if a project API call fails for it, it falls back to the server's default.
- **Desktop orphaned its sidecar server on exit** — a PyInstaller onefile runs
  as a bootloader + re-exec'd child, so the Tauri shell killing the tracked
  process on quit left the real server alive (holding its port; they accumulated
  across open/close cycles). The shell now passes `PROTOAGENT_PARENT_PID` and the
  server runs a parent-death watchdog that exits when the launcher goes away
  (clean quit, crash, or SIGKILL). No-op for standalone/container runs.
- **Lean Docker image (`--ui none`/`console`) couldn't serve** — `fastapi` was
  never declared in any requirements file; it came in only transitively via
  Gradio, which the lean tiers drop (ADR 0010). The lean image therefore had no
  FastAPI and the server couldn't start. Declared `fastapi` in
  `requirements-core.txt` (caught by the runtime-image pytest-collection check).

### Added
- **Eval coverage for the agent layer** (ADR 0012 §2.5): new `subagent` +
  `workflow` eval categories track the research stack. A `workflow` case kind
  drives a recipe end-to-end via `POST /api/workflows/{name}/run` (research-and-brief,
  deep-research) and asserts on its output; `expected_any_tools` asserts the lead
  *delegated* (via `task`/`task_batch`/`run_workflow`) without over-constraining to
  one tool; and `verify_rubric` adds an **LLM-judge** (`evals/judge.py`) that scores
  output against yes/no criteria for quality substrings/audit can't check (is the
  report balanced? is the confidence earned?). Three starter cases added.
- **Eval model comparison + trend tracking** (ADR 0012): every eval report is
  now tagged with the **model under test** (auto-detected from `/healthz`,
  overridable with `--model-label`). A `PROTOAGENT_MODEL` env var overrides the
  YAML `model.name` so the same agent boots against any model. New
  `evals/sweep.py` boots a throwaway `--ui none` agent per model (own port +
  `PROTOAGENT_INSTANCE`), runs the suite against each, and prints a
  `model × category` pass-rate matrix; new `evals/report.py` aggregates every
  model-tagged report into a leaderboard + per-model trend over time. `/healthz`
  now returns the active `model`; `evals/results/` is gitignored.
- **Deep-research workflow with adversarial review** (ADR 0011): a bundled
  `deep-research` recipe (`run_workflow`/`/deep-research`) that orchestrates a
  six-stage DAG — `research ∥ dissent → gap_fill → antagonist ∥ verify →
  synthesize` — to fix the one-sided, self-graded ceiling of a single researcher.
  Three new subagent roles back it: an **`antagonist`** (steelmans the opposing
  case, attacks weak claims, hunts disconfirming evidence), an independent
  **`verifier`** (labels material claims supported/unsupported/uncertain), and a
  **`synthesizer`** that writes a balanced report — folding the opposition into a
  "Counterpoints & caveats" section, dropping unverified claims, and only earning
  a high `Confidence` when the opposition was answered.

### Changed
- **Researcher subagent + web-research skill upgraded** to a proper deep-research
  pipeline (lessons from rabbit-hole.io): scope a question into orthogonal
  **dimensions** (scaled quick/standard/deep), gather with **source
  diversification** (KB reuse + general + community/code) and per-dimension
  compression, run a **conservative gap-check loop** (1-3 genuine gaps, ~3
  rounds), synthesize with **numbered inline citations** (every material claim
  cited, both sides on disagreement), and **persist** one durable finding to the
  KB. The researcher gains `memory_ingest` for that persistence.

### Docs
- **Adopt the shared protoLabs.studio docs theme + brand assets.** The docs now
  use `@protolabsai/vitepress-theme` (maps VitePress `--vp-*` vars to the
  `@protolabsai/design` `--pl-*` tokens, so the site is brand-consistent from one
  source; `appearance: "force-dark"`). The placeholder teal favicon is replaced
  with the canonical protoLabs marks (`favicon.svg` + `protolabs-icon-outline.svg`
  from the design package), and the landing-page feature cards drop their emoji
  icons. The "Built by protoLabs.studio" footer stays (now using the brand
  gradient token).
- **"Built by protoLabs.studio" footer on every docs page** — a custom theme
  (`docs/.vitepress/theme/`) injects a `StudioFooter` via the `layout-bottom`
  slot (the built-in footer hides on sidebar pages), with the brand-gradient
  `protoLabs.studio` wordmark linking to protolabs.studio.
- Reconcile drift after the recent releases: fix the deploy guide's stale
  "every merge auto-cuts a patch" note (releases are manual now), document the
  UI tiers + `--build-arg UI=full` for the image, link the orphaned "Eval your
  fork" guide, and run the OpenShell deploy example with `--ui none`.

## [0.8.0] - 2026-06-01

### Added
- **Headless setup + UI deployment tiers** (ADR 0010): `--ui {full,console,none}`
  (env `PROTOAGENT_UI`). `none` serves API + A2A + `/metrics` only — no Gradio,
  no React console — the lean headless stack. `python server.py --setup` (and
  boot-time auto-complete in the `none` tier) finishes setup from a validated
  config — no wizard. `GET /healthz` readiness probe (503 until the graph
  compiles). `gradio` is now an optional dep (`requirements-core.txt` vs
  `requirements-ui.txt`); the Docker image defaults to the lean tier
  (`--build-arg UI=full` for the all-in-one). `--headless` is a deprecated alias
  for `--ui console`.

## [0.7.0] - 2026-06-01

### Added
- **Playbooks surface** (ADR 0009) — a Knowledge ▸ Playbooks console surface to
  browse + manage the procedural-memory skill index (`skills.db`): pinned
  (SKILL.md) vs learned (agent-emitted), confidence/last-used, search, and
  delete-with-confirm. New API: `GET /api/playbooks` + `DELETE /api/playbooks/{id}`.

### Changed
- **Studio console reshaped to the control stack** (ADR 0009): tabs ordered
  Goals → Workflows → **Run** (Single/Batch is a mode on Run, not a tab);
  **Schedule** moved to **Activity** (it's a trigger, not a work-type). Skills
  now live under **Knowledge ▸ Playbooks**.
- Default model alias is now **`protolabs/reasoning`** (was `protolabs/agent`) —
  forks point at the reasoning model out of the box (override per agent in YAML).

## [0.6.0] - 2026-06-01

### Added
- **Operator primitives** (ADR 0007): a fenced multi-project filesystem toolset
  (`tools/fs_tools.py`) + project registry — opt-in, off by default. Enables a
  fork like Roxy; the agent's own repo is excluded by default.
- **Sandboxing** (ADR 0008): a deny-by-default `egress.allowed_hosts` allowlist
  enforced in `fetch_url`, and `scripts/gen_openshell_policy.py` to generate an
  NVIDIA OpenShell sandbox policy from config (project registry → Landlock
  paths, egress allowlist + gateway → network policy). New guides:
  "Build an operator fork (Roxy)" and "Sandboxing & egress".
- **Run protoAgent under OpenShell** — `deploy/openshell/` managed example:
  gateway compose + a sandbox-create script (Docker), and Helm values + an
  Agent-Sandbox CRD template (Kubernetes), policy generated from config.

## [0.5.1] - 2026-06-01

### Added
- Compaction telemetry signal (`*_compactions_total`, ADR 0006): with routing +
  tool deferral + compaction now all measured, every optimization lever the
  agent has is observable (`/api/telemetry/insights` `unproven_levers` is empty).

## [0.5.0] - 2026-06-01

### Added
- **Observability & the self-improving flywheel** (ADR 0006): measure → persist
  → surface → advise.
  - Per-LLM-call telemetry at the streaming seam: prompt-cache tokens, per-call
    latency, model, and USD cost (`pricing.py`); wired the previously-dead
    Prometheus LLM metrics (calls, latency, tokens, cache, cost).
  - `cost-v1` A2A artifact now carries Anthropic-shaped cache fields + `costUsd`
    and the agent declares the `cost-v1` extension in its card (fleet alignment).
  - Local `TelemetryStore` (per-turn rollups) + read API
    `/api/telemetry/summary` · `/recent` · `/insights`.
  - **System ▸ Telemetry** operator-console dashboard: cost, cache-hit %,
    p50/p95 latency, by-model + recent-turns tables, and an advise-only Insights
    panel (flags ≥5× median cost/latency turns, proves the cache lever in $).
  - Per-turn actual-model routing (`model`/`models`) + a
    `*_llm_tools_deferred_total` Prometheus counter proving tool deferral.

### Changed
- `costUsd` is computed in-process from a pricing table (consumers prefer it
  over recomputing from tokens).

## [0.4.0] - 2026-06-01

### Added
- MCP per-server tool allowlist (`tools.include` / `tools.exclude`) and lazy
  `enabled: false` connect, bounding the per-turn tool-schema footprint
  (ADR 0005 #1).
- Skills surface their declared `tools:` to the agent as `<relevant_tools>`
  when retrieved — a relevance hint, not a gate (ADR 0005 #2).
- Opt-in deferred tools + a `search_tools` meta-tool for progressive tool
  disclosure at high tool counts (`tools.deferred`, ADR 0005 #3).
- `CHANGELOG.md` (this file), following Keep a Changelog.

### Changed
- Releases are now cut **manually** via `workflow_dispatch` (choose
  patch/minor/major) instead of auto-bumping on every merge to `main`.
- `main` is protected by a repository ruleset: a PR and the three CI checks
  (Verify workspace config, Python tests, Web E2E smoke) are required to merge.

### Docs
- ADR 0005 — Tool Pollution & Progressive Tool Disclosure.
- Releasing runbook (`docs/guides/releasing.md`).

---

Releases cut before this changelog was introduced are recorded on the
[GitHub Releases](https://github.com/protoLabsAI/protoAgent/releases) page.

