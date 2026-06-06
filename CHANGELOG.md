# Changelog

All notable changes to protoAgent are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Add your entries under [Unreleased]** in your PR. When a release is cut,
> `prepare-release.yml` rolls them into a dated, versioned section via
> `scripts/changelog.py`. See [Releasing](docs/guides/releasing.md).

## [Unreleased]

### Added
- **Install plugins from a git URL** (ADR 0027, PR1) — `python -m server plugin
  install <git-url> [--ref <tag|sha>]` clones a plugin repo into the live plugins
  dir (already discovered by the loader), **pinned to a resolved commit SHA** and
  recorded in a committed **`plugins.lock`** for reproducible installs
  (`plugin sync` re-clones the exact set). Also `plugin list` / `uninstall` /
  `sync`. Safety baked in: **install ≠ enable ≠ trust** — it only fetches code +
  reads the manifest (data), never imports the plugin and never pip-installs its
  deps (`requires_pip` is declared, installed explicitly); it refuses to shadow a
  built-in, rejects a repo with no manifest, drops git metadata, skips submodules,
  and supports an optional `plugins.sources.allow` allowlist. Manifest gains
  `requires_pip` / `repository` / `homepage` / `min_protoagent_version`. A console
  **Plugins panel** (Settings → Integrations, PR2) installs from a URL, lists
  installed plugins with their manifest + declared capabilities for review, shows
  enabled state + the "enable in config + restart" hint, and uninstalls — backed by
  `/api/plugins/installed|install` + `DELETE /api/plugins/{id}`. PR3 adds the safety
  rails: **`plugin install-deps <id>`** (the explicit, separate pip step) with a
  clear "declared deps not installed — run install-deps" diagnostic when an enabled
  plugin's deps are missing; **audit logging** of install/uninstall/install-deps;
  and a **`plugins.sources.allow`** allowlist (host/org globs) enforced on CLI +
  console installs. See
  [ADR 0027](docs/adr/0027-install-plugins-from-git-url.md).

## [0.19.0] - 2026-06-06

### Added
- **Plugin-contributed console surfaces** (ADR 0026, PR1) — a plugin can declare
  a `views:` block in its manifest (`{id, label, icon, path}`); the console reads
  it from `/api/runtime/status` and renders a **dynamic left-rail icon** whose
  panel is a same-origin **iframe** of the page the plugin serves (e.g.
  `/plugins/<id>/view`) — so a fork gets its own rail dashboard with no console
  rebuild. Surfaces are keyed `plugin:<id>:<viewId>`; chat stays mounted (its
  continuity holds) while a plugin view is open. The `hello` example plugin now
  ships a demo view. The view is hosted by a dedicated `PluginView` component with
  load/error states + a stale-surface fallback (returns to chat if a plugin view's
  plugin is disabled while it's open). A view may declare **`tabs:`** (rendered as
  a sub-nav that swaps the iframe page), and the console hands the hosted page the
  operator **bearer + theme tokens via a post-load `postMessage`** (no token in the
  URL) so it can call its own API and match the console look. The iframe is
  sandboxed (`allow-scripts allow-forms allow-same-origin`). See
  [the guide](docs/guides/plugin-views.md) and
  [ADR 0026](docs/adr/0026-plugin-contributed-console-surfaces.md).

## [0.18.0] - 2026-06-06

### Added
- **Token-by-token answer streaming** (console + A2A). The agent's answer now
  streams into the chat bubble as the model writes it, instead of landing all at
  once at turn end. Two parts: the LLM client runs with `streaming: True` so the
  graph's `ainvoke` streams under `astream_events` (the chat driver already turns
  `on_chat_model_stream` into `("text", delta)` events, scoped to the `<output>`
  region); and the A2A executor now **forwards each text delta as an incremental
  `artifact-update` (append) frame** (lightly batched), then replaces with the
  canonical final text + the cost-v1/confidence DataParts on completion. The
  console already appends artifact deltas, so it fills live with no client change.
  Backward-compatible: a non-streaming model still delivers the answer once at the
  end. (`graph/llm.py`, `a2a_executor.py`; tests in `test_a2a_handler.py`.)

### Fixed
- **Chat self-heals an interrupted stream** (console). A chat turn whose stream
  was cut off — page reload, network blip, or a stale tab — left the assistant
  message stuck "streaming" (spinner) **forever**, even after the agent's turn
  completed server-side. The turn's A2A task id is now persisted on the message,
  and on load a stuck `streaming` message **reconciles against the durable server
  task** (`tasks/get`): it finalizes with the completed answer (flipping any
  running tool cards to done), surfaces a failure, or briefly polls if the turn is
  genuinely still running — instead of spinning indefinitely. e2e:
  `chat-reconcile.spec.ts`.
- **Chat continuity across navigation** (console). Switching from the Chat tab to
  another surface (Activity/Studio/Settings/…) **unmounted** `ChatSurface` — which
  tore down the still-mounted session pool, and its unmount cleanup aborted the
  in-flight stream — so an in-progress turn was lost and the chat appeared to
  reset on return. `ChatSurface` is now rendered **unconditionally** and hidden
  via CSS when off-tab (an `active` prop), so the turn keeps streaming into the
  module-level chat store in the background and the conversation is exactly as you
  left it when you navigate back — the protoMaker always-mounted pattern. Multiple
  chat sessions in the pool all keep progressing. Added a pulsing **background-
  streaming dot** on the Chat rail button (a narrow store selector, so it only
  re-renders on the streaming on/off transition, not per token). e2e:
  `chat-continuity.spec.ts`.

### Fixed
- **Brand favicon** — every surface now shows the canonical protoLabs icon (the
  violet `#9b87f2` bot outline) instead of a leftover Qwen-template placeholder
  (a teal `#14b8a6` "Q" in `static/favicon.svg` + the PWA icons, and an off-brand
  `#7c3aed` outline in the console). Replaced the favicon across `static/`,
  `docs/public/`, and `apps/web/public/` with the brand mark from
  [protoContent](https://github.com/protoLabsAI/protoContent)'s design system;
  fixed the PWA `manifest.json` theme color (`#14b8a6` → `#9b87f2`) and dropped
  `maskable` from the transparent icons. Added a root `/favicon.svg` + `/favicon.ico`
  route so a deployed agent's base URL shows the mark, not a 404. Forks inherit the
  fix on sync.

### Added
- **Unified delegate registry** (ADR 0025, PR1) — a new opt-in `delegates` plugin
  gives the agent one tool, `delegate_to(target, query)`, over a hot-swappable
  roster of the agents and endpoints it can talk to: fleet **A2A agents**,
  OpenAI-compatible **model endpoints** (ask another model), and **ACP coding
  agents**. One adapter per type (the acp adapter reuses the ADR 0024 `AcpClient`;
  the a2a adapter reuses the `peer_tools` JSON-RPC path), each exposing a field
  schema. Declare delegates in a top-level `delegates:` list; editing it +
  Save & Reload swaps the roster live (no restart). Unifies what `code_with`
  (acp) and `peer_consult` (a2a) did and adds model-endpoint delegation. Ships
  disabled; enable with `plugins: { enabled: [delegates] }`. A console panel to
  manage delegates from the UI lands in a follow-up slice.
  See [the guide](docs/guides/delegates.md).
- **Delegate CRUD REST API** (ADR 0025, PR2) — `/api/delegates` (GET/POST/PUT/
  DELETE) + `/api/delegates/test` (reachability probe — agent-card GET for a2a,
  `/v1/models` ping for openai, binary-on-PATH + workdir for acp) +
  `/api/delegate-types` (the field schema that drives the panel). Mutations write
  the config + route each delegate's secret to the gitignored `secrets.yaml`
  (a `delegate_secrets` overlay keyed `<name>.<field>` — never echoed back or kept
  in tracked config), then hot-reload so the new roster is live next turn. Same
  operator-console posture as `/api/config`.
- **Delegate management panel** (ADR 0025, PR3) — a **Delegates** view in the
  console under **Settings → Integrations**: lists delegates with type/secret/
  status badges + a per-row **Test** probe; adds one via a type picker
  (A2A agent / Model endpoint / Coding agent) and a form generated from each
  type's field schema; edits/deletes; secrets entered route to `secrets.yaml` and
  are never echoed back. Saving hot-reloads, so the roster is live next turn. The
  Integrations tab appears whenever the `delegates` plugin is reachable, even with
  no other integration enabled. (`apps/web`; e2e `delegates.spec.ts`.)
- **Delegate health prober** (ADR 0025, PR4) — a background surface probes every
  delegate periodically (initial delay + fixed interval) into a cache that
  `GET /api/delegates` merges in, so the panel shows a **live health dot** (green
  reachable / red down / grey unchecked) per delegate, not just on-demand Test.
  Completes ADR 0025. `code_with` and `peer_consult` are now **deprecated** in
  favor of `delegate_to` (still functional; removed in a future release).

## [0.16.0] - 2026-06-06

### Added
- **Eval-case gating (`requires_env`)** — an eval case can now declare
  `requires_env: [VAR, …]`; when any is unset the case is **skipped** (shown
  `SKIP`, excluded from the pass/fail tally) instead of run, so a case needing an
  optional integration doesn't break the default board. Uses it to ship a gated
  `code_with_delegation` case (ADR 0024) that verifies end-to-end coding-agent
  delegation over a live A2A turn — run it with `EVAL_CODING_AGENT=1` once a
  coding agent is configured. See [Eval your fork](docs/guides/evals.md).
- **Spawn CLI coding agents over ACP** — a new opt-in `coding_agent` plugin
  (ADR 0024) adds a `code_with(agent, task)` tool that hands a real, repo-scoped
  coding job to a purpose-built CLI coding agent (protoCLI `proto`, Claude Code,
  Codex, Gemini CLI) and returns its result. protoAgent is the
  [ACP](https://agentclientprotocol.com) *client* — it launches the agent as a
  subprocess and drives one session over JSON-RPC 2.0 on its stdio
  (`initialize` → `session/new` → `session/prompt`), accumulating the agent's
  message as the answer. The ACP client is a port of ORBIS's canonical
  implementation. Ships **disabled with no agents configured** — each agent gets
  file + shell access in its (config-pinned, auto-allowed) workdir, so it's a
  deliberate opt-in; enable with `plugins: { enabled: [coding_agent] }` and
  declare agents under the `coding_agent` config section. One client (subprocess +
  session) is cached per agent so follow-up calls continue the same thread.
  Synchronous (final answer returned; `tool_call` titles logged).
  See [the guide](docs/guides/coding-agents.md).
- **Coding-agent permission controls** (ADR 0024) — each configured agent takes a
  by-kind permission policy applied to the coding agent's `session/request_permission`
  requests: `auto` (allow all, default), `allowlist` (allow all but
  `execute`/`delete`), or `readonly` (read-like kinds only) — overridable with
  `allow_kinds` / `deny_kinds`. Plus a per-call consent gate (`confirm: true`)
  that asks the operator via `ask_human` before each `code_with` call. Ships
  agent recipes for protoCLI, Claude Code, Codex, and Gemini CLI. (Per-action
  live HITL is deferred — pausing a blocking subprocess session mid-turn is
  incompatible with LangGraph's resume model; use `readonly`/`allowlist` for
  deterministic per-action control.)

## [0.15.1] - 2026-06-05

### Fixed
- **Browser chat rendered blank** (console). The chat turn streams over `/a2a`
  `SendStreamingMessage` and the client hand-parses the SSE body, but
  `drainSseBuffer` scanned for an LF blank line (`\n\n`) while the a2a-sdk
  separates events with **CRLF** (`\r\n\r\n`) — so no frame boundary was found,
  zero frames parsed, and the assistant bubble stayed empty even though the
  agent replied. Now matches any blank-line boundary (`\r\n\r\n` / `\n\n` /
  `\r\r`). Browser-only (the desktop path uses the non-streaming `/api/chat`
  fallback, which masked it); the e2e mock now emits CRLF so CI guards it.
- **Agent name shown as a lowercase slug** in the console (tab title, topbar,
  boot gate, runtime panel). A fork configures a lowercase identity (`gina`,
  `roxy`) because the name doubles as a metrics/API-key/path slug; the UI now
  display-cases it (`gina` → `Gina`) via a `brandName()` helper while keeping the
  `protoAgent` brand and any intentional casing.

## [0.15.0] - 2026-06-05

### Changed
- **Internal: `_main()`'s inline route handlers moved into `operator_api/*`**
  (ADR 0023, phase 3 — composition root down to app assembly). Each route group
  is now a `register_*_routes(app)` function matching the existing
  `register_operator_routes`, so the handler bodies (which only touch `STATE`)
  are testable without booting the server:
  `operator_api/telemetry_routes.py` (`/api/telemetry/*`),
  `knowledge_routes.py` (`/api/knowledge/search` + `/api/playbooks`),
  `config_routes.py` (`/api/config*` + `/api/settings*`), and
  `chat_routes.py` (`/api/chat`, `/api/goal/*`, `/healthz`, OpenAI-compat
  `/v1/*`). The 21 React-console handler closures also moved out — into
  `operator_api/console_handlers.py` — finishing the half-done `operator_api/`
  extraction. Net: **`server.py` went from 3,353 lines to a ~700-line `server/`
  package composition root** (`_main` is ~430 lines of pure app assembly).
  Phase 3 is complete; ADR 0023 is fully shipped.
- **Internal: agent init / builders / reload / settings moved to
  `server/agent_init.py`** (ADR 0023, phase 2 — final backend extraction).
  `_init_langgraph_agent`, the ten `_build_*` component builders
  (knowledge / skills / MCP / plugins / checkpointer / inbox / activity /
  telemetry / workflow / scheduler), the checkpoint-prune + thread-retire loops,
  plugin-host wiring, `_reload_langgraph_agent`, and the operator-console
  settings callbacks (27 functions) now live in their own module.
  `server/__init__.py` re-exports every name and drops ~1,135 lines — the
  composition root is now ~1,355 lines (was 3,353 before phase 1). Pure move
  (1000 tests + a live smoke green: boot exercising every builder, a chat turn,
  and a config-driven hot reload).
- **Internal: the chat backend moved to `server/chat.py`** (ADR 0023, phase 2).
  The LangGraph turn loop — `chat` (Gradio + OpenAI-compat), the streaming
  `_chat_langgraph_stream` (A2A handler), the shared `_run_turn_stream` event
  loop, tool-preview/interrupt shaping, and slash-command parsing/execution —
  now lives in its own module. It imports only neutral modules (no `server`
  symbols), so there's no import cycle; `server/__init__.py` re-exports every
  name. Pure move (1000 tests + a live smoke green: non-streaming + streaming
  turns). `server/__init__.py` drops ~645 lines.
- **Internal: the A2A surface moved to `server/a2a.py`** (ADR 0023, phase 2).
  Agent-card building, skill declarations (`_SKILL_SPECS` + `_agent_skills` +
  `structured_skill_schema`), the per-turn telemetry writer, and the executor
  terminal hook now live in their own module; `server/__init__.py` re-exports
  every name so `server.<symbol>` is unchanged. Pure move (1000 tests + a live
  A2A 1.0 round-trip green). Fork-relevant only if you *monkeypatch*
  `server._SKILL_SPECS` at runtime — patch `server.a2a._SKILL_SPECS` instead
  (editing the source list works as before).
- **`server.py` is now a `server/` package** (ADR 0023, phase 2 prep). The
  monolith moved to `server/__init__.py` (the composition root) with a
  `server/__main__.py` entry, so the backends can be extracted into
  `server/a2a.py`, `server/chat.py`, `server/agent_init.py` next. **Launch it as
  a module: `python -m server`** (was `python server.py`) — the container
  entrypoint, eval sweep, and desktop-sidecar build were updated to match.
  Pure move + the `__file__`→`_bundle_root()` path-anchor fix (the package adds
  one directory level); `import server` / `from server import X` are unchanged
  (1000 tests + a full live smoke green: boot, chat turn, A2A 1.0 round-trip).
- **Internal: `server.py`'s 26 ambient module-globals → an `AppState` container**
  (ADR 0023, phase 1). Runtime state (graph, stores, registries, scheduler,
  MCP/plugin state) now lives in `runtime/state.py` as a named, injectable
  singleton (`STATE`) instead of bare module globals — the foundation for
  splitting the 3,353-line monolith into focused modules. Zero functional change
  (1000 tests + a full live smoke green); fork-relevant if you patched
  `server._<global>` (now `server.STATE.<field>`).

### Changed
- **Semantic recall is on by default.** `knowledge.embeddings` now defaults to
  `true` and `embed_model` to `qwen3-embedding` (what the protoLabs gateway
  serves). The store fuses FTS5 + vector search so it finds paraphrases keyword
  search misses; the circuit breaker degrades to keyword-only if the gateway
  can't embed, so it's safe for forks (set `embed_model` to your gateway's, or
  `knowledge.embeddings: false`).

## [0.14.0] - 2026-06-05

### Fixed
- **Semantic-recall embeddings were non-functional against a real gateway**
  (found by a full knowledge-store smoke test). `create_embed_fn` built
  `OpenAIEmbeddings` with its default client-side tiktoken tokenization, which
  posts `input` as int arrays — a LiteLLM/vLLM gateway rejects that with a 422
  ("input should be a valid string"). Now passes `check_embedding_ctx_length=
  False` so the raw string is sent. Also: the default `embed_model`
  (`nomic-embed-text`) isn't what every gateway serves (the protoLabs gateway
  serves `qwen3-embedding`) — documented that `embed_model` is gateway-specific.
  Verified live: hybrid search now returns a fact via a paraphrased query that
  keyword search misses.

### Added
- **Docs: "Memory & the knowledge store"** (`docs/explanation/`) — the store, the
  three memory types (semantic facts / episodic summaries / procedural
  playbooks), write paths + the reasoning guardrail, retrieval, and how to turn
  on semantic recall (with the gateway-model caveat).
- **Activity is a provenance feed, not a second chat** (ADR 0022). Every
  reactive turn is tagged with *what triggered it* (scheduled job / webhook /
  inbox source / sister-agent / your reply) — the backend tracked this `origin`
  on the A2A metadata but dropped it before the UI, so Activity just showed
  `agent: <text>`. Now `origin`/`trigger`/`priority` ride `TurnOutcome`, land in
  a small `activity` log, and the console renders a timeline where each entry
  shows its trigger badge + time + priority, openable to continue. Answers "why
  did the agent just do that?" at a glance.

### Fixed
- **Inbox `now`-fire was silently broken since the A2A 1.0 migration.** The
  inbox→Activity fire self-POSTed with the retired 0.3 wire shape (`message/send`,
  `role: "user"`, params-level `contextId`, no `A2A-Version` header), which
  a2a-sdk 1.1 rejects with `-32601`/`-32602` — and the fire reported success
  because a JSON-RPC error rides an HTTP 200. So `now`-priority inbox items never
  reached the agent. Migrated to the 1.0 shape (matching the scheduler's fire)
  and the success check now inspects the JSON-RPC error. Found by the Activity
  audit; verified live (a `now` item now fires and lands in the feed).

### Added
- **`fact_recall` eval** — locks the new semantic-fact bucket: a `domain="fact"`
  chunk (what the harvest extractor produces) is passively recalled by the
  KnowledgeMiddleware and surfaced in the answer. Tracked alongside the existing
  recall cases (ADR 0012). The hybrid-vs-keyword recall comparison runs via
  `evals.sweep` with `knowledge.embeddings` on (once the gateway serves an
  embedding model).

### Fixed
- **`<prior_sessions>` can no longer leak reasoning; one loader, not two** (ADR
  0021). The persisted session files (injected each turn as `<prior_sessions>`
  for cross-session recency) stored raw assistant content — so the model's
  `<scratch_pad>` could ride into later prompts. Now stripped at the write
  source *and* at read (defensive for files written by older builds). The two
  copy-pasted loaders in `MemoryMiddleware` and `KnowledgeMiddleware` are
  collapsed into a single `load_prior_sessions` (the duplication the code itself
  lamented). `<prior_sessions>` is kept — it's the only *immediate* cross-session
  recency the checkpointer/harvest don't provide.

### Added
- **Semantic fact extraction — the memory upgrade** (ADR 0021). The session-end
  pass (`conversation_harvest`) now does both halves: the episodic summary *and*
  a semantic pass that distils **durable facts** (aux model — user preferences,
  decisions, stable facts about their projects), consolidates them (skips
  near-duplicates already in the store), and persists them as `domain="fact"`.
  Importance-gated in the prompt — a chatty turn with nothing durable yields
  nothing. Replaces the removed raw per-turn dump with *extract, don't dump;
  background, not hot-path*. Gated by `knowledge.facts` (default on; rides the
  harvest). New `graph/memory_facts.py`.
- **Knowledge chunks carry a `namespace` dimension.** Facts (and any chunk) can
  be scoped to a per-project/owner namespace, so multi-project scoping (ADR 0007)
  is a later *filter*, not a schema migration. Additive nullable column with an
  online migration for existing DBs; `add_chunk`/`add_finding`/`list_chunks` take
  `namespace`, plus a precise `delete_by_id` (backs fact consolidation).
- **Semantic recall: the dormant embeddings layer is now wired** (ADR 0021). The
  `HybridKnowledgeStore` (FTS5 + vector search, RRF-fused, with an embedding
  circuit breaker) and the `embed_model` config existed but were connected to
  nothing — knowledge recall was keyword-only. A new `knowledge.embeddings` flag
  (default **off**) flips `_build_knowledge_store` to the hybrid store with an
  `embed_fn` wired to the gateway (`graph.llm.create_embed_fn`, same OpenAI-compat
  endpoint + WAF-safe UA as the chat model). Off → keyword-only (unchanged); on →
  hybrid semantic + keyword. Any failure degrades to FTS5, never KB-less, and the
  breaker handles runtime embedding outages. Exposed in Settings → Memory.

### Fixed
- **Knowledge store no longer fills with raw reasoning** (ADR 0021). The memory
  middleware dumped *every* assistant turn into the knowledge base — raw,
  truncated at 2000 chars, with the model's internal `<scratch_pad>` reasoning
  intact — which the retrieval layer then recycled into later prompts. That
  per-turn dump is removed (conversation knowledge is captured by the summarized,
  scratch_pad-stripped `conversation_harvest` on thread retirement instead). A
  guardrail at the store's single write chokepoint (`KnowledgeStore.add_chunk`)
  now strips `<scratch_pad>`/`<think>` from *every* writer defensively — internal
  reasoning can never reach the store again. Regression tests added.
- **Settings is its own rail surface; category sub-nav no longer overlaps the
  fields.** The category sub-nav (added with the Settings regroup) landed in the
  `.stage-panel` grid's `1fr` content row, so it stretched over the fields. Gave
  the Settings panel its own `auto auto 1fr` grid (header · sub-nav · scrolling
  body) and promoted **Settings out of System into a top-level rail item** (its
  own view), so it no longer competes with System's sub-nav. System is now
  Runtime · Telemetry.

### Added
- **Knowledge surface = searchable Store + Playbooks** (ADR 0020). The Knowledge
  rail was mislabeled — it showed only Playbooks while the actual knowledge base
  (the `knowledge/store.py` FTS5 chunks: findings, daily-log, harvested sessions,
  operator notes that feed `<learned_skills>`) was unbrowsable. Knowledge now has
  two sub-tabs: **Store** (a searchable view, default) and **Playbooks**. New
  read-only `GET /api/knowledge/search?q=…` endpoint (empty `q` → most-recent
  chunks; non-empty → FTS5 search) backs the Store view. Also a debugging window
  into "why did it recall that?".
- **Subagents are runnable as chat slash commands** (ADR 0020). A message like
  `/researcher find the latest on X` runs the named subagent and returns its
  output — the composer analogue of the `task` tool, so "run a worker" is a
  gesture, not a separate surface. Every registered subagent (built-in + plugin)
  is offered in the `/` autocomplete alongside `/goal` and the workflow
  commands. A workflow of the same name wins; a bare `/<subagent>` shows a usage
  hint; an unknown `/name` falls through to a normal turn. First step toward
  collapsing Studio to Workflows-only (the Run tab becomes redundant).

### Changed
- **Settings regrouped into 5 categories** (ADR 0020). The Settings surface was a
  flat ~12-section scroll mixing model config, cache TTLs, middleware toggles, and
  plugin integrations. Sections now fold into a category sub-nav — **Agent**
  (Identity · Model · Routing), **Behavior** (Compaction · Caching · Goal mode ·
  Tools), **Memory** (Knowledge), **Integrations** (Discord · Google · plugins),
  **System** (Middleware · Runtime). The schema (`build_schema`) tags each group
  with a `category` and orders them; plugin-contributed sections default to
  Integrations. Pure reorganization — no field added or removed.
- **Studio is now Workflows-only; the Run tab is gone** (ADR 0020). The Studio →
  Run panel was a forms-based way to launch a subagent manually — redundant now
  that subagents (and workflows) run as chat slash commands. Studio's rail lands
  directly on Workflows (authoring/inspection); to *run* a worker, type
  `/<subagent>` in chat. Removes `RunPanel` + the Studio sub-nav.
- **Console loading screen: better-styled logo (matches ORBIS).** The launch
  brand splash (`IntroSplash`) and cold-start `BootGate` rendered the bot mark
  as a static `<img>` in the brand-default violet `#7c3aed` — muddy on the dark
  background. Ported ORBIS's inline `ProtoLabsIcon` component (variants
  `flat`/`outline`/`white`, plus a `decorative` a11y prop) and switched both
  screens to the `outline` variant in the lavender chrome accent `#9b87f2`, so
  the mark is a crisp inline SVG that pops against the chrome. Wordmark + glow
  unchanged. (Topbar `brand-mark` + favicon still use the static asset — a
  follow-up if we want full consistency.)

## [0.13.2] - 2026-06-04

### Fixed
- **Eval `ask()` capped every turn at 30s — slow cases ReadTimeout'd.** A2A 1.0's
  non-streaming `SendMessage` *blocks* until the task is terminal (the 0.3
  `message/send` returned immediately and the client polled), but `ask()` still
  built its httpx client with a fixed `timeout=30` — so any turn longer than 30s
  (`web_search`, subagent delegation) raised `ReadTimeout` even when the case
  budgeted 90–300s. The POST now uses the call's `timeout_s`, and a client-side
  timeout returns a clean `state="timeout"` instead of a raw exception. Verified
  live: `research_delegation` now passes at ~92s (was a 30s timeout). Regression
  test pins the constructed timeout.
- **Eval harness spoke the retired A2A 0.3 wire shape — every case failed.** The
  A2A 1.0 migration (ADR 0014) moved the server to `a2a-sdk` (≥1.1), which serves
  proto method names (`SendMessage`/`GetTask`/`SendStreamingMessage`/`CancelTask`),
  requires an `A2A-Version: 1.0` request header (a missing header is read as 0.3,
  so the 1.0 methods 404 with `-32601`), and emits untyped parts (`{"text": …}`,
  no `kind`) with `TASK_STATE_*` states. `evals/client.py` + `evals/runner.py`
  were left on the 0.3 shape (`message/send`, `role: "user"`, `{"kind": "text"}`,
  no version header), so `python -m evals.runner` failed *every* case with
  "method not found". Migrated the eval client/runner to the 1.0 wire shape
  (header + proto method names + `ROLE_USER` + untyped parts + `TASK_STATE_*`
  normalization + the streaming `statusUpdate`/`artifactUpdate` oneof frames +
  `contextId` moved inside the message, where 1.0's `SendMessageRequest` expects
  it — at params level it's a `-32602`, which would have broken goal-mode cases).
  Regression test (`tests/test_eval_client_a2a_1_0.py`) drives the real client
  against an in-process `a2a-sdk` app and pins that the legacy shape is rejected.
- **Plugins: multi-module support.** The plugin loader now imports a plugin's
  `__init__.py` as a package — registered in `sys.modules` before exec with a
  sanitized module name — so a plugin can have sibling modules and use relative
  imports (`from .tools import …`). Previously a hyphenated plugin id produced an
  illegal module name and the relative import failed at load. Regression test added.
- **Discord "Test connection" ignored the entered token** (always reported "bot
  token is empty", even for a valid token). The discord plugin route's request
  model was a *function-local* Pydantic class, but the plugin module uses
  `from __future__ import annotations` (PEP 563) — so the annotation is a string
  FastAPI resolves via `get_type_hints()` against *module globals*, where the
  local class doesn't exist; FastAPI couldn't build the body model and silently
  dropped the body. Moved `DiscordProbe` to module level. (Lesson for plugin
  routes: with PEP 563, body models must be module-level.) Regression test added.

## [0.13.1] - 2026-06-04

### Fixed
- **First-run setup left plugin routes unmounted until restart.** Plugin routers
  (e.g. `POST /api/config/test-discord`, `GET /api/config/google/status`,
  `POST /api/config/google/connect`) mount once at process init — but on a fresh
  pre-setup boot the graph-build path returned early *before* loading plugins, so
  nothing mounted, and completing setup via the wizard reloaded the graph without
  mounting them. Result: a brand-new agent's **Connect Discord / Connect Google /
  Test-connection buttons 404'd during first-run setup** until the app was
  relaunched. Plugins are now loaded for their routes + surfaces even without a
  compiled graph (they need no graph; they're how the wizard *configures* the
  agent), so the routes are live from boot. Found by driving a fresh agent through
  setup against a live server.
- **Model-connection error leaked a token hash into the setup UI.** A bad-but-
  well-formed API key made the gateway (LiteLLM) return a 401 whose body included
  the masked key, an internal **token hash**, and table names — surfaced verbatim
  in the wizard's "Test connection" error. The validator now keeps the actionable
  cause (e.g. "Authentication Error, Invalid proxy server token passed") and
  strips everything from the first secret-ish marker on, so no token/hash/internal
  detail reaches the UI.

## [0.13.0] - 2026-06-04

### Docs
- **agent-card.md corrected against the live card.** Introspected a running
  `/.well-known/agent-card.json` (and the `protolabs_a2a` package): the reference
  now shows the real A2A 1.0 proto shape — `supportedInterfaces` (not a top-level
  `url`), the correct `provider` (`protoLabs AI` / `https://protolabs.ai`), the
  nested `securitySchemes` (`apiKeySecurityScheme` / `httpAuthSecurityScheme`) +
  `securityRequirements`, and all four declared extensions (`cost-v1`,
  `confidence-v1`, `worldstate-delta-v1`, `tool-call-v1`). Dropped the stale
  hand-written literal (flat `securitySchemes`, `stateTransitionHistory`).
- **Docs audit & refresh (24 files).** Swept the docs against current code after
  the Discord/Google→plugins migration and the desktop fixes. Highlights:
  Discord/Google now documented as **first-party plugins** (config lives in
  plugin-declared `discord:` / `google:` sections, not typed fields; disable via
  `plugins.disabled`); `register_mcp_server` + the `--mcp-plugin <id>` frozen
  entrypoint + `host.config()`/`host.apply_settings()` added to the plugins guide;
  the plugin contribution count corrected (five → six) across guide + architecture
  + README. Reference fixes: `configuration.md` gained `tools.disabled`,
  `plugins.disabled`, the plugin-config model, `routing.aux_model`, and the
  `checkpoint` / `workflows` sections, and the **filesystem** defaults corrected
  (now on-by-default + `run_requires_approval`); `environment-variables.md` dropped
  the non-existent `GRADIO_SERVER_*` vars and the wrong "not set by the template"
  claims, and documents the Discord/Google env fallbacks + `PROTOAGENT_*` paths;
  `starter-tools.md` recounted + added `request_user_input`/beads and the
  discord-as-plugin note; `agent-card.md` renamed `_build_agent_card` →
  `_build_agent_card_proto` and reflects the four default extensions. Fixed broken
  fork/deploy instructions (the removed `github.repository` guard → `RELEASE_ENABLED`
  variable; dropped the `sed`-rename anti-guidance) and tutorial drift
  (`WORKER_CONFIG`→`RESEARCHER_CONFIG`, `SYSTEM_PROMPT`→`SOUL.md`, `gh_pr_view`→
  `github_get_pr`). Documented the desktop non-streaming `/api/chat` chat contract
  and the frozen build's plugins/tools bundling in the React+Tauri guide.

### Fixed
- **Desktop chat showed a blank assistant reply (no response).** WKWebView (the
  Tauri shell) doesn't deliver a `text/event-stream` body through `fetch()` at all
  — neither `body.getReader()` nor a buffered `clone().text()` fallback returns the
  bytes — so the streaming `/a2a` turn rendered as an empty assistant bubble even
  though the agent replied. In the desktop shell the chat now uses the
  non-streaming `/api/chat` endpoint (ordinary JSON, which WKWebView handles fine —
  it's how the rest of the console already talks to the sidecar): one request, full
  reply, rendered once. Browsers keep the token-streaming `/a2a` path (with
  tool-call cards). Found by building + driving the desktop app directly.
- **Discord plugin failed to load in the frozen desktop app (`No module named
  'tools.discord_tools'`).** Migrating Discord to a plugin (#513) removed the only
  static import of `tools.discord_tools` from `tools/lg_tools.py`, so PyInstaller's
  import-scan no longer saw it (the plugin imports it, but plugins are loaded by
  file path — invisible to the scan) and it was dropped from the bundle. The
  sidecar build now collects the whole `tools` package, so plugin-only tool
  imports resolve in the frozen app. Caught by running the frozen binary directly;
  the Google plugin was unaffected (its modules are collected via `mcp_servers`).

### Added
- **Plugins can contribute managed MCP servers — `register_mcp_server` (ADR
  0019, #509).** A plugin ships an **MCP server the agent connects to** via a
  factory `factory(config) -> entry | None` called at every graph build — return
  an entry when the server should run, `None` when it shouldn't, so it comes and
  goes with config. Its presence activates MCP even when `mcp.enabled` is off, and
  a same-named entry replaces a configured one. For frozen desktop builds (no
  `python` on PATH), a generic `--mcp-plugin <id>` shim re-invokes the binary and
  runs the plugin's `mcp_main()`. This is what lets the Google surface ship its
  OAuth-gated server as a plugin. The plugin host also gained `host.config()` (the
  live config) + `host.apply_settings(patch)` (persist + reload) so a plugin route
  can read live config and apply a config change.

### Changed
- **Google ingress is now a first-party plugin (`plugins/google`, #509).** The
  Gmail/Calendar managed MCP server, its OAuth-gated launch, the `GET
  /api/config/google/status` + `POST /api/config/google/connect` routes, and the
  `google` config/secrets/Settings group all moved out of `server.py`,
  `tools/mcp_tools.py`, and the core config layer into a self-contained plugin
  (ADR 0019), built on the new `register_mcp_server`. Behaviour is unchanged — the
  Settings group, wizard step, Connect button and live-reconnect-on-save all work
  as before — but a fork can now **disable Google entirely** with `plugins: {
  disabled: [google] }`, or swap in its own integration, with no core edit. No
  config migration: the plugin claims the existing top-level `google` section. The
  desktop sidecar now bundles the `plugins/` tree so the Discord + Google plugins
  load in the frozen app.
- **Discord ingress is now a first-party plugin (`plugins/discord`, #509).** The
  Discord DM gateway, the `POST /api/config/test-discord` route, the outbound
  `discord_*` tools, and the `discord` config/secrets/Settings group all moved
  out of `server.py` + the core config layer into a self-contained plugin (ADR
  0018/0019). Behaviour is unchanged — the Settings group, wizard step, Test
  button and live-reconnect-on-save all work as before — but a fork can now
  **disable Discord entirely** with `plugins: { disabled: [discord] }` (drops the
  surface *and* the tools), or swap in its own ingress plugin, with no core edit.
  No config migration needed: the plugin claims the existing top-level `discord`
  section, so saved tokens/admin IDs keep working.

### Added
- **Plugin host context — `registry.host` (#509 prereq).** A plugin surface/route
  can now reach the **agent invoke** + the **event bus** (`host.invoke(prompt,
  session_id)` / `host.publish` / `host.subscribe`) — host services it can't build
  itself. The server populates a process singleton before any surface starts. The
  last foundation a real ingress surface (Discord-style gateway) needs to live in
  a plugin instead of `server.py`.
- **`plugins.disabled` denylist + plugin surface `reload` hook (#509 prereqs).**
  `plugins.disabled` turns off a bundled first-party plugin even if its manifest
  says `enabled: true` — so a fork drops a built-in surface without deleting it.
  `register_surface(..., reload=fn)` lets a surface reconnect on a config change
  (the server calls `reload(new_config)` on the loop), so a config-driven surface
  keeps live-reconnect instead of needing a restart. Both pave the way for
  migrating the Discord/Google surfaces to plugins (#509).
- **Plugins can contribute config, settings & secrets (ADR 0019, #508).** A
  plugin **declares its config in the manifest** (`config_section` / `config`
  defaults / `secrets` / `settings`) — known at config-load time without importing
  the plugin. It claims a top-level config section and gets: a resolved config
  (manifest defaults ⊕ YAML ⊕ secrets overlay, read via `registry.config`),
  secret routing to `secrets.yaml` (via a dynamic `secret_paths()`), and an
  auto-generated **System → Settings** group — with no `config.py` /
  `config_io.py` / `settings_schema.py` edit. A section colliding with a built-in
  is ignored. Completes the plugin reach (config + ADR 0018's surface/route/
  subagent), so a fork ships a fully self-contained configurable surface as a
  plugin — the prerequisite for migrating the built-in Discord/Google surfaces
  (#509). The `plugins/hello` example now declares a config section + secret.
- **Plugins can contribute surfaces, routes & subagents (ADR 0018, #506).** The
  plugin `register(registry)` contract gained `register_router` (a FastAPI
  `APIRouter`, mounted under `/plugins/<id>`), `register_surface` (a lifecycle
  `start`/`stop` background surface, run on the server loop like the Discord
  gateway), and `register_subagent` (a `SubagentConfig` added to
  `SUBAGENT_REGISTRY`) — on top of the existing tools + skills. So a fork ships
  its own ingress / HTTP endpoint / delegate as a `plugins/<id>/` directory with
  **no `server.py` / registry / `SUBAGENT_REGISTRY` edit** — the last fork
  re-sync friction point. Routes + surfaces wire once at init (a `plugins.enabled`
  change needs a restart); contributions show in `GET /api/runtime/status`. The
  shipped `plugins/hello` example now demonstrates all five contribution types.

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

