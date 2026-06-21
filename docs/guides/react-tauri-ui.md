# Operator console

The operator console is protoAgent's UI: a React + Vite single-page app served at
`/app`, and the same app wrapped as a **Tauri desktop binary** with a frozen Python
sidecar. It's the **default and only** UI — Gradio was removed; `/` redirects to
`/app`. This guide covers running it, its layout, and how its surfaces behave. For the
HTTP it speaks to, see the [Operator REST API](/reference/operator-api); to run with no
UI at all, see [Run headless](/guides/headless).

## Run it

```bash
python -m server                 # console at http://localhost:7870/app  ( / → /app )
```

The server mounts the console when `apps/web/dist/index.html` exists; otherwise it boots
API-only. The `--ui` tier (env `PROTOAGENT_UI`) selects it:

- `console` (default) — the React console at `/app` + the full API/A2A surface.
- `none` — API + A2A + `/metrics` only (headless servers, fleet members). `full` is a
  **deprecated alias for `console`** (the old Gradio tier; it logs a warning).

Build the console from the `@protoagent/web` workspace:

```bash
npm ci
npm run build --workspace @protoagent/web    # tsc + vite build → apps/web/dist
npm run dev   --workspace @protoagent/web     # Vite dev server (proxies the API)
```

The repo ships a prebuilt `dist/`; rebuild after changing console source or pulling
frontend changes. For an isolated dev instance (separate port + data), use
`scripts/dev.sh` (`:7871`).

## Layout

The shell (DS `AppShell`) is a **left rail** of grouped surfaces, a **right sidebar** of
the agent's live state, a **utility bar**, and an optional **bottom panel**. The core rail
surfaces — **Chat**, **Activity** (thread + inbox), **Knowledge** (a searchable store),
**Studio** (workflows), **Agent**, **Plugins**, **Settings** — each fan out to sub-views
via an in-surface segmented control. Enabled plugins add their own views (ADR 0026), each
declaring a placement: `rail`, `right` (right-sidebar panel), or `bottom`. Press **⌘K** /
**Ctrl-K** for the [command palette](/guides/command-palette) to jump anywhere.

The **Agent** surface is the agent's own makeup, tabbed: **Identity** (edit its name +
`SOUL.md` inline — saving merge-applies config + hot-reloads the graph) · **Tools** (live
inventory by source) · **MCP** (servers) · **Subagents** (the delegate roster) ·
**Skills** (the skill index) · **Middleware** (per-turn graph middleware).

The **right sidebar** holds the agent's working state + triggers — **Goals** (standing
conditions, set in chat with `/goal`) · **Tasks** (its task board) · **Schedule**
(cron/one-off fires), plus **Notes** (its notebook, which ships as the `notes` plugin).

In a [fleet](/guides/fleet), the console is slug-routed (`/app/agent/<id>/`) and the hub
reverse-proxies each window to its agent — switch agents in place or open two at once.

## Chat

Multi-session: sessions persist in `localStorage`, hidden ones stay mounted so background
streams keep running, and each carries its own status + goal panel. The composer has
slash-command autocomplete (from `GET /api/chat/commands`) and renders assistant markdown.

- **Live tool-call cards** — each tool the agent invokes streams in as a collapsible card
  (name, running→done/error, input/result), via the `tool-call-v1` DataPart (see
  [Extensions § tool-call-v1](/reference/extensions)).
- **Skill loads** — when the agent loads a skill's procedure on demand it appears as an
  ordinary `load_skill` tool-call card (progressive disclosure, [Skills](/guides/skills)).
- **Mid-turn steering** — send a message while a turn runs and it folds in at the next
  model call ([Mid-turn steering](/explanation/steering)).

Streaming uses A2A **`SendStreamingMessage`** in the browser.

> **Desktop (WKWebView) exception.** WKWebView won't deliver a `text/event-stream` body
> through `fetch()`, so the desktop app detects the shell (`isDesktopWebview()`) and routes
> the turn through the **non-streaming `POST /api/chat`** — one request, full reply,
> rendered once (no live token/tool-card streaming in the desktop chat; browsers keep the
> streaming `/a2a` path).

## Reactive surfaces (ADR 0003)

The console holds one `EventSource` open to `GET /api/events` for its lifetime
(`lib/events.ts`), backed by an in-process `EventBus`. The topbar **live dot** reflects
the connection; producers `bus.publish(...)` and every connected console receives it.

> **Playwright note:** a long-lived SSE connection never lets `networkidle` settle —
> navigate e2e with `waitUntil: "load"`.

- **Activity** (`activity/ActivitySurface.tsx`) — the durable Activity thread: agent-initiated
  turns (e.g. scheduled fires) land here; the operator can reply into the `system:activity`
  context. An unread badge counts events that arrive while you're elsewhere.
- **Inbox** — the read/dismiss view of the authenticated `POST /api/inbox` intake channel
  (webhooks, scripts, sister agents). Items have a `now`/`next`/`later` priority; `now`
  fires an Activity turn immediately, the rest queue for the agent's `check_inbox` tool.

## Agent, Settings & Telemetry

- **Settings** is **schema-driven**: `GET /api/settings/schema` returns fields grouped by
  section (type, value, default, description, `restart` flag); the surface renders inputs
  generically, so new config fields appear without a UI change. Saving POSTs only changed
  fields, writes the YAML (secrets split to `secrets.yaml`), and **hot-reloads the agent
  in-process** — most changes apply without a restart (those that don't carry a `restart`
  badge). Secrets are never echoed (`(set)` / `unset`). Registry: `graph/settings_schema.py`.
- **Telemetry** (Settings ▸ Overview, ADR 0006) — the local per-turn cost/latency rollup
  (totals, by-model table, recent turns) from `GET /api/telemetry/{summary,recent}`.
- **Skills** (Agent ▸ Skills) — browses the skill index: each skill is **pinned** (a
  `SKILL.md` on disk) or **learned** (non-disk, curated), with confidence + last-used, a
  search filter, and delete. (Surfaced via `GET /api/playbooks`.)

## Working memory & the filesystem fence

The agent's stores are **agent-global** — one instance-scoped store each, shared by the
agent's tools and the console (no per-project selector). Tasks lives at `$BEADS_DB_PATH`;
notes ship as the `notes` plugin (`/api/plugins/notes/note`).

`operator.allowed_dirs` in `langgraph-config.yaml` is the **filesystem security fence**
for the agent's file/shell tools (unrelated to notes/tasks): the repo root is always
allowed; add other roots, or set the working dir in the setup wizard's Workspace step.
Out-of-allowlist paths are rejected before any I/O (`..` and symlinks resolved before the
containment check).

## Desktop app (Tauri)

`apps/desktop/` wraps the console as a Tauri v2 binary. `apps/desktop/sidecar/build_sidecar.py`
PyInstaller-freezes the headless server (`binaries/protoagent-server-<triple>`), and
`src-tauri/src/lib.rs` spawns it via `externalBin` with `--ui console` on port `7870`. The
frozen build bundles the `plugins/` tree and `--collect-all`s `tools`/`websockets`/`mcp`
(plugins load by file path, which PyInstaller's scan misses; a runtime-installed comms
plugin, ADR 0058, can only import what's bundled). Signed macOS DMG / Linux AppImage+deb /
Windows NSIS artifacts + an in-app updater ship from the desktop-build CI on release tags.

## Testing the console

A Playwright smoke suite (`apps/web/e2e/`) drives the **built** SPA against a deterministic
mock backend (`mock-server.mjs` serves `dist/` + the API/`a2a` subset from `fixtures.mjs`)
— no Python, model, or network. Specs cover tool-call cards, slash autocomplete, and that
every surface mounts.

```bash
npm run test:e2e --workspace @protoagent/web   # builds, boots the mock, runs headless
```

CI runs it as the **Web E2E smoke** job. When you add a console feature, extend the mock
fixtures + a spec rather than reaching for a live backend.
