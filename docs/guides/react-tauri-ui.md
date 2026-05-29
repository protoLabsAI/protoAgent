# React + Tauri UI Migration

This is the implementation plan for replacing the Gradio UI with a React
operator console, then wrapping it in a Tauri desktop app once the web surface
is stable.

## Source Patterns To Adopt

Use these local references as the starting point:

| Source | Pattern to reuse |
|---|---|
| `/Users/kj/dev/protomaker/projects/ava-chat-system-architecture/` | Architecture notes for multi-session chat, tab systems, client/server contract, and component hierarchy |
| `/Users/kj/dev/protomaker/apps/ui/src/store/chat-store.ts` | Zustand-persisted chat sessions, active-session pool, per-session streaming state |
| `/Users/kj/dev/protomaker/apps/ui/src/components/views/chat-overlay/chat-session-pool.tsx` | Keep multiple chat sessions mounted so background streams continue while hidden |
| `/Users/kj/dev/protomaker/apps/ui/src/components/views/notes-view.tsx` | Multi-tab notes surface with editor toolbar and per-tab agent permissions |
| `/Users/kj/dev/protomaker/apps/server/src/services/beads-service.ts` | `br --json` subprocess boundary; do not read `.beads/beads.db` directly |
| `/Users/kj/dev/protomaker/apps/ui/src/components/views/beads-view/beads-view.tsx` | Beads task list renderer and empty-state init flow |
| `/Users/kj/dev/orbis/web/src/plugins/setup-wizard/SetupWizard.tsx` | First-run onboarding structure and step indicator |
| `/Users/kj/dev/orbis/web/src/plugins/PluginHost.tsx` | Slot-based UI shell that lets major surfaces register cleanly |
| `/Users/kj/dev/protomaker/apps/desktop/src-tauri/` | Tauri v2 tray, global hotkey, hide-on-close desktop wrapper |
| `protoLabsAI/protoContent:docs/reference/visual-identity.md` | protoLabs.studio brand tokens, typography, geometry, and motion rules |
| `protoLabsAI/protoContent:apps/payload/src/app/(frontend)/styles.css` | Deployed marketing CSS variables and dark-first surface treatment |

Important Orbis lesson: Orbis removed Tauri because real-time voice/mic capture
through WKWebView added release risk. protoAgent is text-first, so Tauri is
still reasonable, but add voice later through the browser/PWA path unless the
native media-capture work is explicitly scoped.

## Proto Brand Theme Contract

The React console should use the protoLabs.studio visual identity from
`protoContent` as its source of truth, adapted for dense operator tooling rather
than a marketing page. The useful rule is dark, gray, compact, and precise:
content and work state are the focus; chrome stays quiet.

Brand identity:

- Wordmark text is `protoLabs.studio`: lowercase `p`, capital `L`, dot included.
- Use `protoLabsAI` only for the GitHub organization slug.
- Use `proto-labs.ai` for service hostnames.
- Prefer the outline icon in app navigation and small in-product surfaces. Keep
  neon/large brand treatments for README, splash, install, or about surfaces.

CSS tokens for the first React scaffold:

```css
:root {
  --brand-violet: #7c3aed;
  --brand-violet-light: #a78bfa;
  --brand-indigo: #6366f1;
  --brand-indigo-bright: #818cf8;
  --brand-gradient: linear-gradient(135deg, #a78bfa 0%, #818cf8 50%, #6366f1 100%);

  --bg: #0a0a0c;
  --bg-raised: #131316;
  --fg: #ededed;
  --fg-muted: #8b8b94;
  --border: rgba(255, 255, 255, 0.08);
  --border-strong: rgba(255, 255, 255, 0.18);

  --font-sans: "Geist", system-ui, -apple-system, sans-serif;
  --font-mono: "Geist Mono", ui-monospace, "SF Mono", monospace;
  --radius: 6px;
}
```

Implementation rules:

- Dark-first UI with 14px base text, Geist Sans, and Geist Mono for code,
  metric tags, IDs, and logs.
- Use a 4px spacing grid: 4, 8, 12, 16, 24, 32, 48.
- Keep operator rows at 32-36px and panel padding at 12-16px.
- Use 1px low-contrast borders. Do not add decorative shadows to flat content.
- Use gradients only for brand moments, not buttons, panels, rails, or task rows.
- Keep letter spacing at `0` in the app UI; marketing display treatments do not
  carry into dense console components.
- Do not use glass morphism, backdrop blur, emoji decoration, mascots, or
  rounded-full rectangles. Pills are for badges and avatars only.
- Motion is restrained: 150ms hover, 200ms page transition, 400ms theme switch,
  1000ms linear loading, and 2000ms status pulse. Respect
  `prefers-reduced-motion`.

Status colors should remain semantic and low-chroma: success, warning, error,
and info backgrounds at roughly 15-20% opacity. They should never be used as
ambient decoration.

## Target Shape

Keep the Python/FastAPI/LangGraph backend as the agent runtime. Replace only
the operator surface first.

```
apps/web/                     React + Vite operator console
  src/app/App.tsx             shell, slots, routing
  src/chat/                   multi-session chat pool
  src/setup/                  first-run setup wizard
  src/notes/                  notes tabs/editor
  src/beads/                  task list renderer over br-backed API
  src/subagents/              manual subagent launcher
  src/lib/api.ts              FastAPI/A2A client

server.py                     FastAPI API + static React asset serving
graph/agent.py                LangGraph lead + subagent runtime
src-tauri/                    Tauri shell after web app works
```

Do not remove Gradio in the first slice. Mount React under `/app` or serve it
when enabled by an env flag, keep `/` Gradio until the React app covers setup,
chat, config, and diagnostics.

Web scaffold commands:

```bash
npm run web:dev
npm run web:build
npm run web:preview
```

The built app lives under `apps/web/dist/`. `server.py` serves it at `/app`
when that directory contains `index.html`; otherwise the server boots without
mounting the React surface.

## Required Backend Contracts

The current backend already has useful pieces:

- `GET /api/config/setup-status`
- `GET/POST /api/config`
- `POST /api/config/setup`
- `POST /api/config/models`
- `POST /api/chat` non-streaming
- A2A `POST /a2a` with `message/send`, `message/stream`, and `tasks/get`
- `GET/DELETE /api/goal/{session_id}`

Add these before the React UI depends on them:

| Endpoint | Purpose |
|---|---|
| `GET /api/runtime/status` | setup state, configured model, enabled middleware, knowledge path, scheduler state |
| `GET /api/subagents` | list `SUBAGENT_REGISTRY` entries, tool allowlists, max turns, enabled state |
| `POST /api/subagents/run` | manually launch one subagent with `{session_id, type, description, prompt, emit_skill}` |
| `POST /api/subagents/batch` | manually launch independent subagent jobs concurrently |
| `GET /api/beads/status?project_path=` | detect initialized `.beads/` store through `br list --json` |
| `POST /api/beads/init` | run `br init` idempotently |
| `GET/POST /api/beads/issues` | list/create/update/close/delete issues through `br --json` |
| `GET/POST /api/notes/workspace` | load/save the notes workspace file |

Manual subagents should reuse the existing `_run_subagent` implementation, but
expose it through a service function instead of calling the lead agent's tool.
Keep the one-level delegation guard: manually launched subagents do not receive
`task` or `task_batch`.

## React UI Surfaces

### 1. Shell

Use the Orbis slot pattern:

- `stage`: main work area
- `left-rail`: navigation
- `right-panel`: notes/beads/details
- `overlay-top`: status and connection banners
- `modal`: setup wizard, command palette

This keeps chat, notes, beads, and setup independent instead of building one
large component tree.

### 2. Setup Wizard

Adapt Orbis's wizard structure to protoAgent:

1. Welcome
2. Identity: agent name, operator name
3. Model gateway: API base, API key, model probe
4. Agent persona: SOUL preset and editable SOUL text
5. Tools: middleware toggles, subagent defaults
6. Workspace: memory/knowledge path, optional beads init
7. Finish: write config, mark setup complete, open first chat

Use the existing `/api/config/*` endpoints. Never persist API keys in the
React store; send them only to the backend setup/config API.

### 3. Multi-Chat

Port the Ava chat store and session-pool model:

- persisted sessions in localStorage
- max 50 saved sessions
- max 5 mounted active sessions
- hidden sessions stay mounted while streaming
- per-session status map for background work indicators
- session-scoped goal status panel using `/api/goal/{session_id}`

For streaming, prefer A2A `message/stream` first because it already emits task
state and tool progress. A later pass can add an AI-SDK-compatible `/api/chat`
stream if we want to use `@ai-sdk/react` directly.

### 4. Manual Subagents

Add a panel next to chat:

- choose subagent type from `GET /api/subagents`
- write description + prompt
- launch one job or a batch
- stream tool/status events into a compact task timeline
- insert result into the current chat, save to notes, or emit as a skill

This is different from the lead agent autonomously calling `task()`: the user
can fan out work explicitly when they know the decomposition.

### 5. Notes

Port the ProtoMaker Notes model:

- tab bar with inline rename and protected last tab
- editor toolbar
- per-tab permissions: agent can read, agent can write
- debounced save to backend
- selected readable notes get included in chat/subagent request context

Start with Markdown/plain HTML if TipTap is too heavy for the first slice; keep
the store shape compatible with the ProtoMaker `NotesWorkspace`.

### 6. Beads Task List

Build a Python equivalent of `BeadsService`:

- shell out to `br --json`
- run with `cwd=project_path`
- parse structured errors from stdout/stderr
- expose only JSON DTOs to React
- never inspect `.beads/beads.db` directly

The React renderer should start with:

- init empty state
- create issue row
- grouped task table by status
- priority/type/status badges
- close/start/delete actions

Later: dependencies graph, ready queue, comments, and agent-created issue links.

## Tauri Packaging

Only start Tauri after the React web app works in-browser.

Desktop requirements:

- Tauri v2
- tray icon
- global hotkey to show/hide
- hide-on-close
- bundled static React app
- Python sidecar or "connect to existing local server" mode
- OS-standard data dirs mapped to memory/knowledge/config paths

For the first desktop cut, prefer connect-to-local-server mode. Bundling and
supervising the Python sidecar is a separate packaging problem and should not
block React UI validation.

## Migration Slices

1. **API prep**: add runtime/subagent/beads/notes JSON contracts with tests.
2. **React scaffold**: Vite + React + TypeScript under `apps/web`, served at `/app`, with the proto brand theme tokens above.
3. **Setup wizard**: port the Orbis flow using protoAgent config steps.
4. **Chat shell**: port Ava chat store/session pool; use A2A streaming.
5. **Manual subagents**: add launcher and batch runner UI.
6. **Notes + beads**: port notes tabs and build the `br` task renderer.
7. **Tauri shell**: wrap `/app`, add tray/hotkey/hide-on-close.
8. **Gradio retirement**: remove only after React covers setup, config, chat, diagnostics.

## Risks

- A2A streaming events are not AI SDK data-stream events; the first chat UI
  should consume A2A directly instead of forcing `useChat`.
- Long-running hidden chat sessions need explicit caps and stop controls.
- Manual subagent launch must inherit audit/tracing/session IDs or debugging
  becomes harder than autonomous `task()` calls.
- Tauri packaging can consume a lot of time. Keep it behind the working web UI.
- Do not copy Orbis voice/Tauri assumptions into this app; protoAgent is a
  text-first agent console.
