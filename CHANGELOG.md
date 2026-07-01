# Changelog

All notable changes to protoAgent are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Add your entries under [Unreleased]** in your PR. When a release is cut,
> `prepare-release.yml` rolls them into a dated, versioned section via
> `scripts/changelog.py`. See [Releasing](docs/guides/releasing.md).

## [Unreleased]

## [0.77.0] - 2026-07-01

### Added
- **Cross-machine fleet hardening — A2A federation is fault-transparent** (#1468, #1476) — a peer
  delegate (`delegate_to` over A2A) no longer cuts off a long-running task at a fixed 30s: the poll
  loop runs to a configurable `poll_timeout_s` (default 300s) so the delegator waits while the peer
  keeps working. Transport + protocol failures now map to a legible cause — *unreachable* vs *timed
  out* vs a clear `VERSION_NOT_SUPPORTED` (instead of an opaque `-32009`) — and the agent card
  advertises its A2A `protocolVersion` / `supportedVersions` so a delegate pre-checks compatibility
  and fails fast on a version mismatch.
- **Remote fleet members surface their health immediately** (#1470) — registering a remote
  (`POST /api/fleet/remotes`) now probes its agent card on the spot and returns reachability +
  version (an unreachable peer is reported, not silently accepted), the running-state probe TTL
  tightened to match the console poll, and the delegate health prober backs off exponentially so a
  flaky peer degrades gracefully instead of ping-ponging.
- **Discovery auto-sweeps on hub boot** (#1471) — the hub kicks off a background discovery sweep at
  startup (mDNS + tailnet + local) and caches the peers it finds, so the first console *Add to
  fleet* is instant instead of waiting for a manual scan. Best-effort; peers are only surfaced,
  never auto-added.
- **`config explain` diagnostic** (#1475) — `python -m server config explain` (and
  `GET /api/config/explain`) print this instance's id, both roots, every resolved on-disk path, and
  the per-field settings cascade with provenance (App → Host → Agent), secrets redacted. The
  supported way to answer "where is my config / where did my key go".
- **Real multi-instance fleet test harness** (#1467, #1472) — a real-subprocess integration harness
  (opt-in `PA_RUN_INTEGRATION=1`) boots an actual hub + members and exercises the proxy round-trip,
  cross-instance A2A delegation, instance isolation, and member crash → detect → restart — the live
  multi-agent coverage the fleet previously had none of.
- **Artifact is now a bundled core plugin, on by default** (#1443) — the generative-UI surface
  (`show_artifact` — charts, diagrams, Mermaid, Markdown, or live React rendered into a sandboxed
  panel; ADR 0038) is vendored in-tree under `plugins/artifact/` and ships with the agent enabled,
  a first-party surface like notes/docs (turn off per-instance via `plugins.disabled: [artifact]`).
  Folds in a pointer-lock fix so game/canvas artifacts can capture the pointer.
- **Artifact render errors feed back to the agent** (#1458, artifact plugin 0.12.0) — when a React
  (or other) artifact throws at render time or never mounts, the sandbox now reports the error up,
  and `show_artifact` / `update_artifact` / `rewrite_artifact` surface it inline in their reply
  (*"⚠ But it FAILED to render: Icon is not defined"*) when the panel is open. A new
  `check_artifact` tool returns the latest render verdict on demand. Closes the code→render→fix
  loop so the agent self-corrects instead of guessing. The wait is gated on a live panel, so
  headless/closed-panel runs never block.
- **Multi-step wizard + choice-card HITL forms** (#1464) — `request_user_input` now renders
  multiple `steps` as a real sequential **Back/Next wizard** (step indicator, per-step
  required-field validation) instead of one scrollable form, and supports AskUserQuestion-style
  **option cards** — a field with `oneOf: [{const, title, description}]` renders as selectable
  cards (single-select; `type: "array"` for multi-select), alongside the existing
  text/number/boolean/enum fields. See [Starter tools](/reference/starter-tools).
- **The agent can ingest documents & media into its knowledge base** (#1479, #1485) — a new
  `knowledge_ingest(source, …)` tool pulls a URL (a web article or a **YouTube** link), a PDF, or a
  local audio/video/image file through the full ingestion pipeline (transcripts, gateway STT,
  extraction) and chunks + embeds it for recall — so handing the agent a link or a recording
  actually processes it instead of falling back to a web search. Anything that fetches over the
  network or transcribes media runs in the **background** (ADR 0050) so a long video never blocks the
  chat; a small local text file ingests inline. See [Ingest documents & media](/guides/ingestion).

### Changed
- **Two-tier instance paths (box / instance) — one resolution rule, no more double-scoping**
  (#1463, #1465) — every on-disk location is now resolved once from the environment into a single
  injectable model (`infra.paths.InstancePaths`) with three tiers mirroring the settings cascade:
  **App** (read-only bundle seed), **Box** (machine-shared: the Host config layer + commons), and
  **Instance** (per-agent: config, secrets, plugins, every store). `PROTOAGENT_HOME` relocates an
  instance's root; `PROTOAGENT_INSTANCE` names one under the box; neither → `default`.
  `PROTOAGENT_CONFIG_DIR` is **retired** (desktop, Docker, and fleet members now set
  `PROTOAGENT_HOME`), and live config is never written into the repo tree. This removes the
  config-vs-data root split and the `PROTOAGENT_CONFIG_DIR`+`PROTOAGENT_INSTANCE` collision that
  required a destructive self-heal. **Existing installs upgrade with no action** — a one-shot,
  idempotent, non-destructive boot migration copies old-layout config + secrets (and the default
  instance's data) into the new location. Use `config explain` to see the resolved layout.
  Every data store (checkpoints, knowledge, memory, scheduler, inbox, activity, telemetry, audit,
  tasks, a2a, workflows, …) now lives under the instance root; the Host config layer is box-shared
  (one machine-wide `host-config.yaml`, the layer's intent); shared commons stay shared; and the
  legacy `scope_leaf` scoping knob is removed. (ADR 0065; supersedes the path mechanics of ADR
  0004/0041 and re-amends the host-file location in ADR 0047.)
- **React artifacts are more forgiving + the render loop is proactive** (artifact plugin 0.13.0) —
  the most common first-try mistake (defining a component but never calling `render()`) now just
  works: name your top-level component `App` and the harness **auto-mounts** `<App/>` when nothing
  mounted itself (an explicit `render()` still wins; it never double-mounts). `check_artifact` now
  waits briefly for the verdict when the panel is live (so an immediate post-render check returns
  the real result), and the skill instructs the agent to **verify the render after every create/
  edit** and iterate until it's clean.

### Fixed
- **Docs reader: in-content cross-reference links route in-app instead of breaking the iframe**
  (#1456) — clicking a cross-reference link inside a rendered doc page used to navigate *inside*
  the embed frame, loading a bare page stripped of the docs nav/search in a cramped frame. The
  reader now intercepts content-link clicks: a link that resolves to a bundled doc — relative
  (`./adr.md`, `../guides/skills.md`) or VitePress abs-rooted (`/adr/0060-…`, `/guides/`) — opens
  **in-panel** (carrying any `#anchor`, with client-side heading slugs so anchors land); anything
  else (external, or a doc not in the bundle) opens at the live docs site in a new tab. In-page
  `#section` links scroll the reader instead of reloading it.
- **A crashed co-located fleet member is now detected and restartable** (#1474) — a member the hub
  spawned is its child process, so a SIGKILL crash left it a zombie that `os.kill(pid, 0)` reported
  as *alive*: `/api/fleet` kept showing it running and a restart no-op'd on the dead pid. `_alive()`
  now reaps the zombie first (a targeted `waitpid`, so it never steals another child's exit status),
  so the crash is detected passively and a restart spawns a fresh process. (Surfaced by the new
  multi-instance crash→restart test.)
- **Autonomous turns no longer deadlock on a human-input pause** (#1464, #1466) — a
  `scheduler` / `inbox` / `webhook` / `background` turn that calls `ask_human` /
  `request_user_input` has no operator to answer, so the task used to park in `input-required`
  forever (a state exempt from the TTL sweep). It now auto-answers the pause with a "no
  operator — proceed" sentinel (bounded), and past that budget **force-completes** the turn —
  clearing the stray interrupt — rather than parking. Live operator and inbound-`a2a` turns
  still park as before. Dismissing a HITL card now resolves the parked task instead of only
  clearing it client-side.
- **HITL tools are hard-denied to subagents** (#1469) — `ask_human` / `request_user_input`
  (resumable only by the lead turn's runner) can no longer be bound to a subagent even if a
  `SubagentConfig.tools` allowlist names one — enforced in `_subagent_tools`, not just
  convention. `request_user_input` also rejects an empty `steps` list instead of silently
  degrading to a free-text box.
- **Settings surfaces when the agent config shadows a host-scoped field** (#1459) — when a
  `scope="host"` field (e.g. `model.api_base`) is set in both `host-config.yaml` and the agent
  leaf (`langgraph-config.yaml`), the agent value wins at runtime (ADR 0047) but the host console
  used to badge it a plain "box default", hiding the override. It now shows an **"overridden by
  agent config"** warning with Reset-to-inherited (which removes the agent override so the box
  default applies), and config load logs a warning naming each shadowed key.
- **The ⌘K command-palette chat survives being closed mid-turn** (#1487) — closing the palette used
  to abort the turn and lose it; it now pins the server task id and, on reopen, reconnects to the
  still-running turn (or shows its finished result) via the same durable `tasks/get` self-heal the
  main chat uses.

### Docs
- **Knowledge: fleet/commons sharing + the reusable background-job primitive** (#1477, #1488) —
  documented sharing a knowledge store across a fleet (the private/commons tiering + the console
  Share/Unshare gesture), corrected the stale `knowledge.top_k` default (5 → 10), and added a
  "Background jobs" guide covering `task(run_in_background=true)` and the
  `BackgroundManager.spawn_work` primitive for detaching deterministic long work.

## [0.76.0] - 2026-06-30

### Added
- **"Manage plugins…" in the rail context menus** (#1426) — right-clicking empty rail space or any
  rail icon now offers **Manage plugins…**, which opens the plugin manager (Settings ▸
  Integrations). It's the all-plugins counterpart to a plugin icon's per-plugin *Configure…*.
- **Reveal toggle on secret fields** (#1442) — every masked secret/token input (settings secrets,
  delegate auth tokens, the operator-token gate, MCP server secrets, the setup-wizard API key) now
  carries an eye button to show what you typed or pasted, so you can verify a key before saving.

### Changed
- **Settings true-up — one canonical config system** (#1428, #1432–#1442; ADR 0048 §6) — the
  console's settings, plus the Playbooks and Knowledge surfaces, were unified onto the canonical
  `/api/settings` cascade and TanStack Query (no more bespoke `/api/config` writers or hand-rolled
  fetches), and a wave of console controls moved to the shared `@protolabsai/ui` design system
  (button loading states, toast positioning, icon search inputs, segmented category filters, secret
  inputs). Mostly invisible, but settings and list surfaces now load, error, and behave consistently.

### Fixed
- **Settings surfaces no longer swallow load/save errors** (#1430, #1431) — the Skills, MCP,
  Plugins, and Knowledge surfaces now report a failed load or save via a toast instead of failing
  silently.
- **Identity name and fleet delegates save through the canonical settings cascade** (#1428) —
  retired the last two `/api/config` writers, so these fields persist like every other setting
  (host/agent scoping, hot reload) instead of via a side path.

## [0.75.0] - 2026-06-29

### Added
- **Egress allowlist in Settings** (#1422) — the outbound-host allowlist (`egress.allowed_hosts`,
  ADR 0008) is now editable in **Settings ▸ Box ▸ Network**, the outbound counterpart to the
  inbound *Bind interface*. Host-scoped and hot-reloading; previously YAML-only.

### Fixed
- **Custom model gateway no longer blocked on the connection test** (#1422) — pointing the *API
  base URL* at a local gateway (Ollama / LM Studio / local vLLM / LiteLLM on `localhost`, or a
  LAN/tailnet host) failed with "api_base host is blocked by the egress guard". The connection-test
  probes now allow private/loopback hosts for the operator-configured gateway (still blocking
  link-local / cloud-metadata / multicast / reserved), and when an egress allowlist *is* set the
  configured gateway host is permitted automatically.
- **Plugin config appears without a restart** (#1423) — a newly installed or enabled plugin's
  configuration section now shows up in Settings immediately. The console refetches the settings
  schema whenever the active plugin set changes (install / enable / disable / uninstall / sync /
  update) instead of serving a stale cache until the next app restart.

## [0.74.0] - 2026-06-29

### Added
- **Bypass-permissions mode** (#1418) — a per-tab toggle that auto-approves `run_command` so the
  agent runs shell commands without the HITL approval prompt: `/bypass on|off`, a DS warning badge
  in the composer while it's on, and an **"Approve & don't ask again"** button in the approval
  dialog. Every bypassed command is audit-logged, and a host can forbid bypass entirely via
  `filesystem.bypass_allowed: false`.

### Changed
- **`run_command` runs shell operators** (#1419) — the fenced `run_command` tool executes via
  `/bin/sh -c`, so `&&`, `|`, `>`, and `$(…)` work instead of being literalized by argv-splitting.
  No new capability (the agent could already nest `bash -c "…"`); still cwd-fenced and
  approval/bypass-gated, and a timed-out command now kills its whole process group.

### Fixed
- **Slash-command notices render as system notes** (#1420) — local in-thread notices (e.g. the
  `/effort` and `/bypass` confirmations) are now tone-aware `role:"system"` notes instead of fake
  assistant messages, so they no longer carry the answer action row (copy/fork/regenerate).

## [0.73.0] - 2026-06-29

### Added
- **Background batch delegation** (#1396) — `task_batch(run_in_background=True)` fans a whole batch
  of subagents out detached, returning job ids immediately while you keep working, with each
  completion notified back independently. A new background concurrency cap (default 3, override
  `BACKGROUND_MAX_CONCURRENCY`) bounds how many background turns run at once so a wide fan-out can't
  overload the gateway.
- **Live tool-card feed for background agents** (#1402) — expanding a running background job in the
  Background-agents dialog now follows its tool-by-tool activity live, each step shown as a tool card
  with name, status, and output preview, instead of only the last-three collapsed pills.

### Changed
- **Settings ▸ Knowledge split into sub-sections** (#1408) — the 22-field Knowledge panel is
  organized into **Recall · Ingestion · History** accordion groups instead of one wall, and
  every settings panel now opens its first group by default (no more landing on a fully
  collapsed panel).
- **Tools view — MCP tools grouped by server** (#1405) — MCP tools (namespaced
  `<server>__<tool>`) now group under the server that serves them, mirroring the plugin
  grouping, instead of one flat "MCP" bucket; the group sorts after core + plugin groups
  with an `mcp` source chip on its header.
- **Settings IA — domain-first (ADR 0048)** (#1393) — the settings dialog is reorganized by what a
  setting *does*: an **Agent** group (Identity · Operator & access · Model · Behavior · Knowledge ·
  Integrations), a **Capabilities** group (Tools · MCP · Skills · Subagents · Delegates), a host-only
  **Box** group (Overview · Fleet · Telemetry), and a device-local **This console** group (Theme ·
  Chat · Keyboard). Scope (host vs agent) is a per-field inheritance badge, not a navigation axis;
  sharing/box-runtime knobs are contextual chips on their managers rather than empty panels. Removes
  the dead "two scope homes" axis and the unused Host-defaults panels, and folds Telemetry into the
  single Settings door (no separate drawer shortcut).
- **Tools view — grouped by plugin + subsystem** (#1397) — plugin tools now group under the plugin
  that contributed them (Artifact, GitHub, …) instead of one flat "Plugin" bucket, and the core
  "General" bucket is split into Filesystem / Skills / Web & research subsystems. Groups order
  core → plugin → MCP, with the source shown once on each group header instead of on every row.
- **Built-in subagents answer natively** (#1411) — the built-in subagents (researcher, antagonist,
  verifier, synthesizer, dream, distill) no longer carry the retired `<scratch_pad>`/`<output>`
  protocol directives; they deliberate with native reasoning and return plain answers, matching what
  the lead agent already does. Prompt-text only — no behavior-contract change for fork callers.
- **CI off the deprecated Node 20 action runtime** (#1391) — bumped every GitHub Actions pin across
  the nine workflow files to the lowest major that runs natively on Node 24, so runs no longer log
  GitHub's "Node.js 20 is deprecated" annotation. Notable non-`+1` jumps where the next major was
  still Node 20: `upload-artifact` v4 → **v6**, `build-push-action` v5 → **v7**, and
  `attest-build-provenance` v1 → **v3** (its v2 leaf `actions/attest` was still Node 20). All are
  pure-runtime bumps for our usage — no input/behavior changes; the new majors need Actions Runner
  ≥ 2.327.1, which GitHub-hosted runners (all we use) already satisfy.

### Removed
- **Structured-output parser retired** (#1412) — the dead `<scratch_pad>`/`<output>` XML parser is
  deleted (`graph/output_format.py` shrinks 473→67 lines), completing the move to native model
  reasoning. The lead agent and subagents already stopped emitting the protocol; this drops the
  no-longer-used `<output>` extraction, the dropped-turn retry, the `<confidence>` self-report (and
  its A2A DataPart / chat-stream event), and the streaming-view machinery. Forkers keep only a thin
  leaked-reasoning strip plus the `<think>`/`<scratch_pad>` guards that stop reasoning from being
  persisted (ADR-0021) or leaking into answers.

### Fixed
- **Concurrent same-conversation turns no longer corrupt chat history** (#1410) — two
  near-simultaneous A2A messages on the same context now run one-at-a-time via a per-conversation
  lock instead of racing and losing history, and a reasoning-only model that emits no answer now
  surfaces its last tool output or a placeholder instead of a silently blank reply.
- **Chat answers no longer truncated; A2A tasks return the real final answer** (#1409) — an answer
  that mentions a protocol tag like `<scratch_pad>` in inline code is no longer cut off in the
  stored / A2A / Discord copy; and when the canonical final answer diverges from the streamed text
  (goal-outcome notes, retries, reshaping), the durable A2A task artifact is replaced with the true
  answer instead of keeping stale streamed deltas, so `tasks/get` and delegating agents see the
  correct result.
- **Subagent token streams isolated from the live chat** (#1394) — running concurrent subagents
  (`task` / `task_batch`) no longer garbles the chat stream or pollutes the lead's final answer;
  subagent reasoning and draft output stay off the main turn and return only via the delegation tool
  card, while tool-card nesting and cost accounting stay intact.
- **Cross-tab chat no longer clobbers itself** (#1413) — two browser tabs of the same agent share one
  chat-store key, and the last tab to write used to overwrite the other's chats, silently losing
  conversations. Tabs now union-merge their sessions (newest edit wins; live-streaming and
  just-deleted chats stay authoritative) and sync each other's chats live.
- **ACP coding-agent eviction race closed** (#1406) — concurrent chat turns no longer corrupt the
  per-thread ACP runtime cache, and idle/LRU eviction never tears down a runtime whose turn is still
  streaming, so a long coding turn can outlive the 30-minute idle TTL without being killed by an
  unrelated turn.
- **Cross-context streaming guard** (#1399) — the console drops any streaming frame whose `contextId`
  doesn't match the active chat turn, so a stray frame from a concurrent turn or a detached
  background job can't render into the wrong message. Frames without a `contextId` pass through, so
  older servers and the A2A 0.3 shape are unaffected.
- **File uploads restore the token prompt on auth failure** (#1404) — `requestForm` read the response
  body twice in its error path, throwing "body stream already read" — which masked the real HTTP
  error (e.g. "file too large") and, on token-gated deployments, skipped the 401 AuthGate so uploads
  never prompted for a token. The body is read once now, surfacing the true error and re-enabling the
  sign-in prompt.

### Security
- **Secure defaults for metrics and MCP secrets** (#1395) — `/metrics` is no longer unconditionally
  public: on a token-gated deploy it requires `Authorization: Bearer <token>` (or
  `PROTOAGENT_PUBLIC_METRICS=1` to keep anonymous scraping), and stdio MCP subprocesses no longer
  inherit credential-looking env vars by default. **Breaking (token-gated deploys only):** Prometheus
  scrapers must authenticate, and an MCP server relying on an implicitly-inherited secret must set
  `inherit_env: true` or pass it via a per-server `env:` block. Local tokenless deploys are
  unaffected.
- **Backend launch-hardening — ingestion SSRF guard + credential hardening** (#1398) — web/file
  ingestion now runs the same egress allowlist as `fetch_url` (redirects disabled and re-checked per
  hop), closing server-side fetches of cloud-metadata and internal hosts into the knowledge base;
  plus constant-time API-key/inbox-token comparison, PEP 508 validation of plugin pip deps to block
  flag/VCS injection, HITL pauses preserved through the TTL sweep, and SQLite `busy_timeout` on the
  knowledge / scheduler stores.
- **Plugin auth-bypass and event-loop hardening** (#1401) — a plugin can no longer strip the bearer
  gate off core routes (including the install/RCE route): `public_paths` match on namespace subtrees
  and plugin IDs are validated against a reserved-name denylist. The `data` goal-verifier's `eval()`
  is AST-guarded against attribute-traversal sandbox escapes, and plugin install/update/sync run off
  the asyncio loop so one operator install no longer freezes all chat / A2A / scheduler traffic.
- **Fail-safe plugin secret redaction** (#1403) — when plugin config discovery hits a transient
  failure, the server fails safe instead of fail-open: `GET /api/config` blanks the entire affected
  plugin section rather than echoing its secrets, and cached secret paths are preserved so a plugin
  secret can't be written into the exportable main YAML in plaintext.
- **Operator-token storage guidance** (#1414) — documents where the console's operator bearer lives:
  the server env (`A2A_AUTH_TOKEN`) is the recommended home. The browser console caches a copy in
  `localStorage`, an accepted residual bounded by the localhost-default bind, default-deny bearer
  gate, and sanitized-markdown-only rendering — rotate the token on compromise and don't expose the
  console beyond localhost without a fronting proxy.

## [0.72.0] - 2026-06-28

### Added
- **Context-window meter + per-turn cost/time** (#1372) — the chat header shows a live
  context-window usage meter, and each completed turn reports its token cost and wall-clock time,
  so you can see how close a conversation is to the model's window and what each turn spent.
- **Vision-describe pass for text-only models** (#1381) — attach images to a chat whose model has no
  native vision: a describe pass turns each image into a text description the model can reason over,
  instead of dropping the attachment.
- **"Get models"** (#1386) — a Settings action that pulls a gateway's advertised model list and
  populates the Primary model dropdown, so you pick from what the gateway actually serves instead of
  typing model ids by hand.
- **Inline components re-enabled** (#1323) — an extensible registry with clean, deterministic
  ordering replaces the disabled inline-component path, so plugins can contribute inline chat
  components again.
- **Per-stimulus Activity attribution** (#1375) — each Activity response is attributed to the
  specific stimulus it replies to, so the reactive thread reads as paired stimulus → response
  instead of an undifferentiated stream.

### Changed
- **Inline action feedback → toasts** (#1389) — settings and seven panels now surface transient
  action results (save / test / connect / CRUD) as DS toasts instead of inline status lines,
  continuing the toast sweep.

### Fixed
- **Deduped inbox/Activity now-item notifications + deliver-before-fire** (#1375) — now-item
  notifications no longer double-fire across the inbox and Activity surfaces, and a delivery now
  lands before its fire event.
- **Clear error for images on a text-only model** (#1374) — attaching an image to a text-only model
  now shows a clear, actionable error instead of a cryptic extractor rejection.
- **Chat-tab trash only on the hovered ✕** (#1373) — the delete affordance shows on the ✕ you're
  hovering, not on every tab at once.

## [0.71.0] - 2026-06-27

### Added
- **Panel-focus keybindings** (ADR 0063) — `⌃1`/`⌃2`/`⌃3`/`⌃4` move keyboard focus *into* the
  chat composer / left panel / right panel / bottom dock (so that region's scoped binds activate).
  Literal `⌃` (mac) so they're distinct from `⌘1–9` tab-jump; `⌃2/3/4` land on the first
  interactive element in the dock. Rebindable in Settings ▸ Keyboard.

### Changed
- **⌘K palette chat streams with live text↔tool interleave** — PaletteChat now builds the same
  ordered `parts` the main chat does (via the shared `appendText`/`appendReasoning`/`addToolRef`
  helpers + the top-level-only `addToolRef` rule), so the shared `<ChatMessageView>` renders the
  interleaved timeline (and WorkBlock fold) live instead of the grouped history-fallback. Full
  parity with the main chat "as it's doing its thing."
- **Streaming answer text is full-width, no loading side-bar** — removed the DS streaming-pulse
  (animated 2px accent left-border + inset) from the streaming message body, so the answer streams
  as raw, full-width text instead of behind an animated rail. Applies to the main chat and the ⌘K
  palette chat; tool cards keep their own loaders.
- **No hardcoded emojis in the UI** — stripped emoji/glyph literals from user-facing strings: the
  chat paste-attachment label (`📎` → `Attached:`), background-agent completion headers (`✅`/`⚠️`),
  the `/effort` notes (`⚙`/`⚠`), delegate/plugin-install status strings (`✓`/`✗`/`⚠`), and the
  background-job tool glyphs (now lucide icons). Status is carried by text/tone/icons, not emoji.

### Added
- **Full-screen document viewer** (ADR 0062) — a reusable reader (`openDocument(spec)` → a
  root-mounted full-screen dialog rendering markdown). Background-agent reports no longer strand
  you: the chat card keeps the preview but a **"Read full report"** button opens the *full* report
  (fetched by job id) full-screen, and **Activity feed** entries open into the *same* viewer — no
  trip to the Background/Activity panel. `DocumentSpec` is generic (inline `content`, async `load()`,
  or a custom `render()`), so future long-content views can reuse it.
- **Keyboard shortcuts** (ADR 0063) — a scoped, user-rebindable keybinding system. Defaults: `⌘K`
  command palette, `⌘,` Settings, `/` focus composer, VS Code-style panel toggles `⌘B` left rail /
  `⌘⌥B` right panel / `⌘J` bottom dock, and (in the chat panel) `⌘T` new chat, `⌘⇧K` clear,
  `⌃Tab`/`⌃⇧Tab` prev/next, `⌘1–9` jump to chat tab N. Bindings are **focus-scoped** (the chat ones
  fire only when the chat panel is focused) and **rebindable** in **Settings ▸ Keyboard** (record /
  reset / conflict-detect; overrides persist globally). Forks/plugins add their own via
  `registerKeybinding`. Note: the browser-mirroring combos (`⌘T`/`⌘1–9`/`⌃Tab`/`⌘B`/`⌘J`) work in
  the desktop app; a browser tab reserves some — rebind to a free combo there.
- **Quick-delete a chat tab** — **Shift+click** a tab's ✕ to delete it with no confirmation dialog
  and no knowledge harvest; while Shift is held the ✕ shows as a red trashcan to signal it. Plain
  click keeps the confirm dialog.
- **Hide a rail surface without disabling its plugin** (ADR 0035/0036) — `railOrder` gains a
  `hidden` bucket: a surface is on exactly one dock *or* hidden (enabled-but-not-shown). Right-click
  a rail icon → **Hide** to declutter the rails without disabling the plugin; restore it from ⌘K,
  from **right-clicking the empty rail** (a "Hidden views" menu), or "Move to …". The reconcilers
  respect `hidden`, so a reload never resurrects a hidden view and uninstalling the plugin prunes
  it. Persist migration **v13**.
- **Configure a plugin from its rail icon or util-bar widget** (ADR 0036/0059) — right-clicking a
  plugin view's rail icon, or its util-bar widget pill, now offers **Configure…**, which opens that
  plugin's settings dialog (the same per-plugin dialog the Plugins manager uses), store-driven from
  a single root mount.
- **Chat tab context menu** (ADR 0036) — right-click a chat session tab for **New chat / Rename /
  Close** (Close reuses the delete-confirm; Rename opens the inline tab editor).
- **Fork-safe console behavior seams** (ADR 0061, #1337) — give the console the backend's
  "extend-without-editing-core, update-safe" property. Extends the `src/ext/` fork pattern
  with three registries mirroring `registerSurface` (static, first-wins, HMR-safe), so a fork
  adds chat behavior by dropping a `src/ext/` module — no core edits, no upstream conflicts:
  - **`registerSlashCommand`** — own a client-side `/<name>` (registering claims the token;
    the frontend twin of the backend's `register_chat_command`). Core's `/new`, `/clear`,
    `/effort` now register through it — no hardcoded verbs remain.
  - **`registerComposerAction`** — add a control to the chat composer's actions slot.
  - **`registerPaletteCommand`** — add a root ⌘K command; core's deep-links (Plugins: Discover,
    Settings, …) are dogfooded through it (no `deepLinkCommands()` bypass).
  - **`createUISlice(namespace, initial)`** — own a namespaced, per-agent-persisted zustand
    store for fork UI state, without editing core `uiStore.ts` (a standardized fork store, not
    a merge into core's `UIState`).

## [0.70.0] - 2026-06-24

### Added
- **Plugins can own `/<name>` chat control commands** via `registry.register_chat_command(name, handler)`
  — the generalized form of the core `/goal`. The handler is `async (rest, session_id) -> str | None`:
  a reply string short-circuits the turn (the model never runs), `None` passes through. It is
  **user-only by design** (not an agent tool), so a plugin can expose a write action the model can't
  trigger autonomously. Precedence is `goal` > plugin command > workflow > subagent > skill, resolved
  once in `graph/slash_commands.py` so the chat dispatcher and the console palette can't drift. This is
  the seam that lets the GitHub `/issue` command move into a plugin.
- **"Report a bug" link in the hamburger menu** (the header side panel), next to Docs /
  Changelog / GitHub — opens the repo's new-issue chooser in a new tab. A lightweight,
  always-present way to file a bug, independent of any GitHub plugin.

### Removed
- **GitHub is no longer in core — it's a standalone plugin** ([`protoLabsAI/github-plugin`](https://github.com/protoLabsAI/github-plugin)).
  Removed the read tools (`tools/github_tools.py`), the `/issue` command logic (`tools/gh_issue.py`),
  its REST surface (`operator_api/github_routes.py`), the in-tree `plugins/github` shim, the core
  `github.repos`/`github.default_repo` config + settings fields, and the console's util-bar
  "New issue" button + dialog (`NewIssueDialog`/`issueBody`). The chat `/issue` command and the
  console GitHub surfaces now come from the plugin (install it + `plugins.enabled: [github]`); the
  same `github.*` config keys carry over. Kept in core: the generic `ci` goal verifier and its
  `tools/gh_cli.py` runner (goal-system infra, not the GitHub toolset). Closes the lean-core audit's
  "GitHub → plugin" item.

## [0.69.0] - 2026-06-24

### Changed
- **Native reasoning — the agent's thinking now streams from the model, not a forced text
  protocol.** Dropped the `<scratch_pad>`/`<output>` convention; the chat renders the model's
  native `reasoning_content`, tool calls, and answer as they actually stream. An agentic
  turn's reason→tool steps fold into one "Working… / Worked" block that tallies reasoning
  steps, tool calls, and skill loads (hover for the breakdown) so the final answer leads;
  the most-recent tool stays spotlighted while the turn runs. (#1328)
- **Chat markdown now renders through the design system's `Markdown` renderer**
  (`@protolabsai/ui`), replacing the hand-rolled pipeline. Assistant answers are
  streaming-hardened (partial markdown never flashes broken mid-stream), with on-brand
  code/table chrome (copy button), KaTeX math, GFM tables/task-lists, and themed mermaid
  code blocks. (#1329, #1331)

### Fixed
- **Heavy research turns no longer wedge the server.** `web_search` and `fetch_url` ran their
  blocking work (DuckDuckGo search, HTML parsing) directly on the event loop, so a parallel
  `task_batch` fan-out could peg CPU and make the server unresponsive — even to cancellation.
  Both now run off the loop, keeping the server responsive under load. (#1328)

## [0.68.0] - 2026-06-23

### Added
- **`scripts/reset.sh` — factory-reset the default (prod) instance from the CLI.** Wipes
  the prod instance's data + local config back to a clean slate so the next boot runs the
  setup wizard (for testing the fresh-user flow). Safe on a multi-instance machine: every
  *other* instance (any `~/.protoagent/<name>` with an `.instance-uid`, the dev sandbox,
  fleet members) and every scoped `<store>/<instance>` leaf is preserved — only prod's
  unscoped DBs + direct files are removed; tracked `config/` files are `git checkout`-
  restored, gitignored local config deleted. `--dry-run` prints the exact plan;
  `--keep-secrets` / `--include-dev` / `--backup` / `--force` / `--yes`. No in-app reset
  (deliberately CLI-only). (#1159)

### Changed
- **Chat tool-call rendering overhaul.** A `task` delegation card now shows which subagent
  ran (`task → researcher`); the subagent's own tools nest inside that card with a running
  count (expand to see them); a turn's finished tools fold into one expandable "N tools"
  summary chip; and the live tool block holds a stable height — the column no longer grows
  and shrinks as tools stream in and out. The summary chip is the new `@protolabsai/ui`
  `ToolCardSummary` primitive. (#1319, #1320, #1321, #1322)
- **`show_component` (inline component rendering, ADR 0051) is temporarily disabled** — not
  in the agent's tool roster or the console Tools tab. The component-v1 pipeline (codec,
  wire extraction, console renderer) is left intact; tracked by #1323. (#1324)
- **The Goals and Tasks panels refresh on a bus push instead of polling every 5s.** Both
  panels held a 5s `refetchInterval`; now the goal store publishes `goal.changed` (on
  set/advance/clear) and the task store publishes `task.changed` (on create/update/
  close/delete), and the panels invalidate off those `/api/events` pushes — the same
  pattern the inbox panel already used. Live updates are now immediate (the agent files a
  task → it appears at once) and steady-state polling is gone. (#1310)

### Fixed
- **Failed or user-cancelled `task` delegations now close as error cards (the X).** They
  returned a plain `Error:` / `[cancelled]` string that rode the green "done" card (the
  card's error flag is read from the tool-message status, which a string never set); the
  tool now returns a `status="error"` ToolMessage so the card matches the red body. (#1319)
- **A subagent's own tool calls reliably nest under the delegation card.** Nesting was
  inferred from frame timing ("last open task wins"), which broke when the detached
  delegation's end raced ahead of its child frames (and mis-attributed concurrent
  `task_batch` delegations); the child frames now carry the parent delegation's tool-call
  id so nesting is explicit and order-independent. (#1321)
- **Mid-stream output rendering no longer rescans the whole response on every chunk.**
  The chat stream recomputed the visible `<output>` (and the live reasoning view) by
  re-running regexes over the *entire* accumulated text per token chunk — O(N²) over a
  turn. New incremental `StreamingOutputView` / `StreamingReasoningView` scan only the
  newly-appended tail (a cheap pre-`<output>` scan, then fast-append in the steady
  answer body), falling back to the authoritative parser on any tag boundary; an
  equivalence + fuzz test pins them byte-for-byte to the original functions. (#1310)
- **Empty-rail panel toggles fully disable.** The utility-bar left/right panel-toggle buttons are now greyed out and non-interactive when their rail holds no views (matching the bottom-dock toggle), instead of appearing active but doing nothing when clicked. (#1234)
- **Console-poll handlers no longer block the event loop.** `GET /api/runtime/status`
  shelled out to `ps` (the per-poll co-location + fleet version-skew probes) and the
  inbox/activity console handlers ran sync SQLite reads/writes directly on the loop; both
  are now offloaded via `asyncio.to_thread`, matching the scheduler/goals handlers and the
  startup-path co-location check. (#875)
- **The Docker image now serves the React console and stays in dep-lockstep with
  pyproject.** A new node builder stage builds `apps/web/dist` and copies it into the
  runtime image, so `-e PROTOAGENT_UI=console` actually mounts `/app` instead of silently
  404'ing (`.dockerignore` no longer drops the workspace manifests the build needs); the
  server now warns loudly when the `console` tier is requested but the console build is
  absent. A new `tests/test_requirements_core_sync.py` guard fails CI if
  `requirements-core.txt` (what the image installs) misses any core `pyproject`
  dependency — it had silently lost `pypdf`, `youtube-transcript-api`, and
  `markdown-it-py`, now restored. (#874)

## [0.67.0] - 2026-06-22

### Added
- **Per-agent ACP launch overrides are honored.** `acp.agents.<name>.{command,args}` is
  now parsed (it was silently dropped — `LangGraphConfig` had no `acp_agents` field), so an
  `agent_runtime: acp:<agent>` turn launches the locally-installed adapter you configured
  (`claude-agent-acp`, `codex-acp`) instead of always falling back to the `npx -y …` fetch
  default — faster cold start, no per-spawn network dependency (ADR 0033). (#1289)

### Fixed
- **ACP delegate health probe is `initialize`-only — it no longer opens a session every
  120s.** The prober ran a full `session/new`/`session/load` against every ACP delegate on
  a timer despite documenting itself as side-effect-free; it now runs only the `initialize`
  round-trip (`AcpClient.handshake()`). The ACP launch env also strips the inherited
  `CLAUDECODE` / `CLAUDE_CODE_*` markers so a spawned Claude backend doesn't refuse to start
  "inside another Claude Code session", and the round-trip (initialize → session/prompt →
  request_permission) is now logged so an idle freeze is diagnosable. (#1301)
- **macOS desktop app finds Homebrew/nvm/Volta/asdf-installed binaries.** A Finder/Dock
  launch inherits only `launchd`'s minimal PATH, so `npx` and ACP adapters were invisible
  and a `delegate_to` coding-agent launch failed with `binary not on PATH`. The bundled
  server is now launched with the user's real login-shell PATH, and the delegate Test probe
  resolves the command against the same merged PATH the spawn uses. (#1302)
- **Workspace port assignment skips OS-occupied ports.** `_pick_port` now bind-probes
  `127.0.0.1` and scans a bounded range, skipping ports held by *unrelated* processes (a dev
  server, another fork on `:7871`) instead of handing out an already-bound port that killed
  the spawned agent with `EADDRINUSE`. (#1290)
- **The per-agent theme is instance-scoped.** `theme.json` now lands under
  `config/<instance>/` like config/secrets/setup, so a `PROTOAGENT_INSTANCE` sandbox no
  longer shares — and clobbers — the default instance's theme (ADR 0042). (#1294)
- **The browser tab favicon + `theme-color` follow the active per-agent theme.** Switching
  agents recolors the tab favicon and PWA/mobile browser chrome to the agent's accent
  instead of always showing the brand default (ADR 0042). (#1297)
- **ACP coding-agent subprocesses no longer leak as orphaned processes.** `delegate_to`
  and the delegate health prober spawn CLI coding agents (`codex-acp`, `claude-agent-acp`,
  …) over ACP, but teardown signalled only the direct child — the backend each adapter
  spawns reparented to init and survived — and `dispatch` awaited a *pooled* client that
  it never reaped on cancel, so stopping a turn left the agent running ("I stopped the
  main thread and the delegate didn't stop"). Over days these piled up to hundreds of
  `ppid 1` orphans holding ~20 GB. Now the agent is spawned in its own process group and
  teardown SIGTERM→SIGKILLs the whole group; `dispatch` hard-kills + drops the pooled
  client synchronously on cancel; the `_start` handshake self-reaps if it fails or is
  cancelled mid-flight (the prober's probe-timeout path); and a shutdown hook drains every
  pooled client so a server stop strands nothing.
- **Dialogs no longer render their content cramped flush to the body edge.** The shared
  DS dialog defaulted to a tight 16px body padding, and roomier dialogs (MCP catalog,
  New-skill) each hand-added a 24px override — so every newly-converted dialog (the
  add-delegate form, schedule, the new task dialog) shipped cramped until someone
  remembered to bump it. Raised the app-wide `.pl-dialog__body` default to 24px once and
  dropped the now-redundant per-dialog overrides. Surfaces that embed a full panel
  (Settings, theme quick-pick) keep their intentional zero padding. (#1288)

### Docs
- **Coding-agents guide: Codex needs the `codex-acp` adapter.** Documented that recent
  `codex` CLI dropped the native `acp` subcommand (it speaks MCP), so it must be driven
  through `@zed-industries/codex-acp`. (#1287)

## [0.66.0] - 2026-06-21

### Changed
- **The agent's task board is now "tasks", not "beads."** The in-process board — the
  console panel *and* the agent's task tools — was never the real `br` beads: it's a
  lightweight SQLite tracker with no dependency graph (the real `br` DAG lives in the
  opt-in `project_board` plugin). It's renamed throughout to end the confusion: the
  console **"Beads" panel → "Tasks"**, the API `/api/beads/*` → `/api/tasks/*`, and the
  agent tools `beads_create`/`beads_list`/`beads_update`/`beads_close` →
  `task_create`/`task_list`/`task_update`/`task_close`. New issue ids are `task-N`
  (existing `bd-N` ids keep working). **Breaking** if you called the old API paths or
  tool names. (#1283)
- **Create a task from a dialog.** The Tasks panel's always-visible inline create form is
  replaced by a "New task" action that opens a dialog (title · type · priority ·
  description), so the board stays the focus. (#1284)

### Removed
- **Dropped the dead `br` fallback from the core task board.** It was a remote `br`
  adapter that only bound for forks not wiring the in-process store; the core is now
  purely the in-process store. Forks wanting real `br` use the `project_board` plugin
  (which already wraps it). (#1283)

## [0.65.0] - 2026-06-21

### Added
- **Schedule view: open a job to read the full prompt + edit it in place.** Clicking a
  scheduled job now opens a detail dialog with the full (un-truncated) prompt, the
  human-readable + raw schedule, next/last fire, timezone and id. An Edit mode changes the
  prompt and/or schedule via a new atomic `PUT /api/scheduler/jobs/{id}` — id, created_at
  and last_fire are preserved and next_fire is recomputed — instead of a cancel-then-re-add.
  (#1277, #1278)
- **Chat: reasoning renders inline, in emission order.** "Thinking" now interleaves with the
  answer text and tool calls (reason → tool → reason → answer) instead of being hoisted into
  a single block at the top of the turn. (#1276)

### Changed
- **Deleting a scheduled job confirms first.** The Schedule view's row trash button and the
  detail dialog's Delete now summon a confirmation dialog (naming the job) instead of deleting
  on a single click. (#1280)

### Removed
- **Removed the Workstacean scheduler backend.** The bundled sqlite `LocalScheduler` is now
  the only backend; the opt-in remote adapter and its `SCHEDULER_BACKEND=workstacean` /
  `WORKSTACEAN_*` env vars are gone (stale vars are ignored). The A2A cost-v1 / effect-domain
  extension is unaffected — its wire URIs stay `proto-labs.ai`-branded. (#1278, #1279)

### Fixed
- **Chat: assistant text and tool calls render in emission order.** A pre-tool preamble
  ("let me look that up") used to render *after* the tool cards because the message
  grouped all text below all tool cards; it now renders above them with the answer
  below (interleaved render blocks). The server also flushes buffered answer text before
  a tool frame, so the preamble reaches the console first — making the in-place streaming
  visible as it arrives rather than appearing to land after the tools. (#1272)
- **Settings: Host-console edits stop "resetting."** A host-scoped field saved on the Host
  console (e.g. the gateway base URL) was silently shadowed by an unmodified copy seeded into
  the agent layer, so it appeared to reset. A host save now clears the shadowing agent-layer
  key, the example config no longer seeds those fields, and a fully-commented-out config
  section no longer crashes the loader. (#1273)
- **ACP: `load_skill` works through the operator sidecar.** The operator MCP server — a
  separate process exposing this agent's tools to an ACP brain — built every store except the
  skills index, so `load_skill` returned "Skills index is not available." even when the prompt
  listed the skill. It now builds the index like the host process. (#1274)
- **Chat: no stray gap between tool calls.** A whitespace-only delta the model emitted between
  two tool calls rendered an empty block and split the tool group into separate cards; it's now
  dropped, keeping consecutive calls grouped. (#1275)

## [0.64.3] - 2026-06-20

### Changed
- **Settings: the Add/Edit delegate form is now a dialog** instead of rendering inline
  in the Delegates panel and pushing the list down.
- **Settings: the New/Edit skill dialog has more breathing room** — roomier padding and
  more space between fields.

## [0.64.2] - 2026-06-20

### Changed
- **MCP: the "Browse common servers" card grid now fills the dialog height** instead of
  capping short and leaving dead space below it; the search/filter row stays pinned and
  the grid scrolls.

## [0.64.1] - 2026-06-20

### Fixed
- **Desktop: the common-MCP-servers picker is no longer empty.** `config/mcp-catalog.json`
  wasn't bundled into the packaged desktop app, so "Browse common servers" showed "no
  servers match"; the sidecar build now ships it (with a regression guard).

### Changed
- **Settings: the "Host · box defaults" badge moved into the dialog header** next to the
  Settings title, instead of sitting atop the body where it pushed the panel content down.
- **MCP: more breathing room in the "Browse common servers" dialog** — the search and card
  grid no longer sit flush against the panel edge.

## [0.64.0] - 2026-06-20

### Added
- **Quick-add for common MCP servers.** Settings ▸ MCP has a "Browse common servers"
  picker — a curated directory (filesystem, git, fetch, GitHub, Brave Search, memory,
  sequential-thinking, time) that one-click adds a server, prompting only for the path
  or API token it needs. Backed by `config/mcp-catalog.json` + `GET /api/mcp/catalog`.
- **Share MCP servers across the box (commons).** A new `mcp.scope` (scoped · layered)
  lets an agent also run the box-shared MCP commons (`~/.protoagent/commons/mcp-servers.json`),
  unioned with its own servers — private wins by name (ADR 0041, mirroring how skills &
  knowledge share). In Settings ▸ MCP, layered servers show a commons/private tier badge
  with one-click share / unshare, and a sharing-mode quick-set sits by the header. A
  shared server runs on every layered agent on the box, so it only adds servers you trust
  box-wide.

### Changed
- **Desktop in-app updater shows the curated changelog.** The updater's release notes now
  come from the hand-written `CHANGELOG.md` section for the new version (the
  `### Added/Changed/Fixed` markdown) instead of auto-generated commit subjects, falling
  back gracefully when a section is empty. First applies to this release. (#1263)

## [0.63.1] - 2026-06-20

### Fixed
- **Plugin manager: uninstall restored.** A regression had left git-installed plugins
  installable and toggleable but not removable; uninstall works again, gated on the
  lock-backed plugin inventory. (#1255)

### Changed
- **Consolidated plugin manager.** Plugin management now lives in one surface, and
  "Install from URL" became a dialog opened from the Installed toolbar (ADR 0059). (#1255)
- **Settings host cue.** The full-width host inheritance banner is now a compact
  "Host · box defaults" badge by the settings header. (#1256)
- **Execute Code plugin card.** Trimmed its catalog-card description (~2.3×) so it fits
  the display, keeping the headline use and the "isolation, not a true sandbox" caveat. (#1257)

## [0.63.0] - 2026-06-20

### Added
- **Shared knowledge tier.** A promotion-defined commons read by every agent on the box — hybrid
  (FTS5 + vector) with an embedding circuit-breaker — surfaced in the console (Knowledge ▸ Store)
  with tier badges and inline share/unshare. (#1248, #1252)
- **Skills: progressive disclosure + tiered curation.** An always-on `<available_skills>` index with
  on-demand `load_skill` (ADR 0060) replaces the old per-turn BM25 retrieval; a tier-aware
  `skills curate` (the commons is dedupe-only); and the Shared Skills panel folded into the Skills
  view with share/unshare there. (#1235, #1246, #1245)
- **Late-tools plugin seam.** `register_late_tool_factory` lets a plugin contribute a tool that needs
  the fully-assembled toolset — the extension point behind moving `execute_code` to a plugin. (#1240)
- **Desktop download page** on the marketing site — a macOS `.dmg` with OS detection and a
  newsletter gate for Windows/Linux. (#1236)

### Changed
- **`execute_code` is now an opt-in plugin** (`plugins/execute_code`), out of the lean core's default
  tool surface. **Migration:** enable it with `plugins.enabled: [execute_code]` instead of
  `execute_code.enabled: true` (the `timeout` / `tools` / `output_truncate` settings carry over under
  the plugin's `execute_code` config section). Its docs now describe it honestly as a sandboxed
  Python interpreter — the `tools` allowlist scopes the convenience bridge, not a security boundary.
  (#1240, #1241, #1243, #1244)
- **Honest middleware surfaces.** Removed the dormant tool-output `ingest` middleware (nothing
  consumed it); made `enforcement` a code/YAML fork seam hidden from the console (its bare toggle was
  a no-op without a policy); and renamed `MemoryMiddleware` → `SessionSummaryMiddleware`, making it
  write-only so `KnowledgeMiddleware` solely owns `<prior_sessions>` injection — correcting docs that
  still claimed it wrote findings to the knowledge store. (#1238, #1239, #1247, #1249)
- **Leaner default skill bundle** — dropped the release-notes skill from the core bundle. (#1251)

### Fixed
- **Skills hardening.** Hardened the shared-skills commons (promote guards, a `forget` CLI, tier
  visibility, docs) and made the ACP feed respect `skills_top_k=0` while capping the `load_skill`
  hint. (#1242, #1237)

## [0.62.0] - 2026-06-20

### Fixed
- **The Knowledge rail icon no longer disappears.** A core rail surface (Knowledge, Work, …)
  missing from a saved layout is now re-added on load — `railSurfaces()` previously only restored
  plugin views, so a layout saved before a surface existed (or that dropped one) silently lost its
  icon, with no migration to bring it back. This is now a general safety net for every core surface.
  (#1230)
- **The active tab's underline is the brand accent again, not white.** Adopted the upstream
  design-system fix (`@protolabsai/ui` 0.45.1) — every `<Tabs>` surface (e.g. the Work hub) now
  marks the active tab with the accent. (#1229)

### Changed
- **Removed the "This is the memory the agent retrieves into context…" footer** from the Knowledge
  panel. (#1230)
- **Docs accuracy pass.** Corrected the starter-tools reference (the default tool set no longer
  lists plugin or retired tools — notes/github/discord/peer aren't in `get_all_tools`) and closed
  feature-coverage gaps (ACP full-tool-parity, the middleware chain, and the artifacts capability).
  Also retired the misnamed `tools/peer_tools.py` → `tools/a2a_parse.py`. (#1228, #1231)

## [0.61.0] - 2026-06-20

### Changed
- **An ACP coding-agent runtime now gets protoAgent's full toolset by default.** Under
  `agent_runtime: acp:<agent>` the external coding agent *is* the brain, so it now has every
  tool — parity with the native runtime, where the gateway model does. `operator_mcp.tools`
  is now an optional *restriction* rather than a required allowlist (empty = everything, minus
  the redundant `execute_code` the coding agent already has), so a skill handed to the coding
  agent can actually run its `web_search`/`fetch_url`/… tools instead of getting a procedure it
  can't execute. The chat also labels the active runtime ("`<agent>` · coding agent") instead
  of the gateway model that never ran the turn. (#1224)
- **Removed the redundant "working…" status strip above the chat composer** — the spinner +
  status readout is covered by the inline turn indicators now. (#1225)

## [0.60.0] - 2026-06-19

### Added
- **The app side drawer now has a Changelog link.** A *Changelog* entry joins Docs/GitHub in the
  drawer's Links section and opens the marketing-site changelog
  (`agent.protolabs.studio/changelog`) in a new tab. (#1220)

### Changed
- **Goal mode is always on.** Its on/off controls are removed from the operator console — the
  Overview "Goal mode" metric, the "Enable goal mode" Settings toggle (now `ui_hidden`), and the
  `goal` block in the `/api/runtime/status` response. The config field stays (default on) so
  existing configs round-trip; the `set_goal` tool, goal controller, and `/api/goals*` endpoints
  are unchanged, and the tuning knobs (max continuations, verifier model) remain editable. (#1222)

### Fixed
- **The frozen desktop app now bundles `config/skills`.** The PyInstaller sidecar shipped every
  read-only config default *except* `config/skills`, so the skill index had nothing to seed from
  at `_MEIPASS/config/skills` in the packaged desktop build. It's now included alongside SOUL.md
  and the other bundled config. (#1221)

## [0.59.0] - 2026-06-19

## [0.58.0] - 2026-06-19

## [0.57.0] - 2026-06-19

## [0.56.1] - 2026-06-19

### Fixed
- **The in-app ⌘K palette no longer inherits the desktop launcher's frosted styling.** The
  launcher window's CSS (transparent scrim, translucent backdrop-blur card, large shadow) is
  bundled globally, so it leaked onto the main console's command palette; it's now scoped to
  the launcher window.
- **Plugin entries in the palette dropped their "open here" hint.** It collided with the new
  `Open…` command (and the shared "open" keyword surfaced every plugin when you typed "open").

## [0.56.0] - 2026-06-19

### Changed
- **The command palette (⌘K) is now command-driven.** The root list leads with **Agents**,
  then **Plugins** (each plugin's views), then **Commands** — the built-in surfaces no longer
  flood the top. An **Open…** command morphs into an `Open ▸` submorph (a searchable list) to
  pick a surface, so the root stays a short list of actions rather than a wall of places. The
  same structure backs the desktop ⌥Space launcher (ADR 0057).

## [0.55.1] - 2026-06-19

### Changed
- **The desktop quick launcher (⌥Space) is now a frosted, rounded floating panel.** The
  launcher window is transparent + shadowless and the palette renders as a translucent,
  blurred, rounded card with see-through margins — a Raycast-style glass look — instead of
  filling the window edge-to-edge.

## [0.55.0] - 2026-06-19

### Added
- **Chat can dock at the bottom panel.** Drag it there, or right-click the Chat rail icon →
  *Move to bottom dock* — previously chat was confined to the left/right rails. Its slot mounts
  unconditionally on the bottom dock the same way it does on a side rail, so an in-flight turn
  keeps streaming when you switch the bottom dock to another surface and back (#613). (Collapsing
  the dock still tears the stream down — same as collapsing a side rail; the conversation itself
  is restored from the session store.)

### Fixed
- **The chat "still streaming" pulse now shows on the right rail and bottom dock.** The rail
  icon's background-stream dot was computed off the left rail only, so it never lit when chat
  lived on the right rail (or the new bottom dock). It's now derived on whichever dock holds chat.

## [0.54.0] - 2026-06-19

### Added
- **Raycast-style global quick launcher (desktop).** A new system-wide hotkey (⌥Space)
  summons a frameless, always-on-top window from anywhere — even while protoAgent is hidden
  in the menu bar — that hosts just the ⌘K command palette: jump to any surface or plugin
  view, run the deep-link actions, quick-chat with the agent, or open an inline plugin view.
  Navigation commands hand off to the main console window, and the launcher dismisses on blur
  or Escape (ADR 0057). `⌘⇧P` still toggles the full console window.

### Changed
- **Activity is a read-only utility-bar widget, off the left rail.** The provenance feed —
  what the agent did on its own, and why — moved from a rail surface into the bottom-left
  widgets cluster, alongside the inbox and background jobs: a pill with an unread badge that
  opens the feed in a dialog. The reply composer is gone; Activity is a read-only event log now.

### Fixed
- **Background agents widget no longer needs a page reload to appear.** The utility-bar pill
  mounts while a cold backend is still warming up (the desktop sidecar can take ~a minute),
  so its one-shot startup fetch could fail before the engine was up and the pill stayed
  hidden until a manual reload. It now re-checks whenever the event bus (re)connects — the
  pill appears as soon as the engine is reachable, and also refreshes after a server restart.

## [0.53.0] - 2026-06-19

### Added
- **Docs plugin — read and ask about protoAgent's own docs** (first-party, on by default).
  A keyword FTS index over the bundled docs + `docs_search` / `docs_read` tools + a skill
  (search → read → cite) so the agent answers from the docs; plus a console **Docs** reader
  view (a Diátaxis→domain tree mirroring the docs site + server-rendered markdown) and a ⌘K
  **Docs** search. Self-contained and offline — no embeddings, no knowledge-store coupling.
- **`user_only` skills** — mark a skill so it's *only* a `/<slash>` command and is never
  auto-retrieved into context, for deliberate run-on-demand procedures.

### Changed
- **Desktop update notice is now a full modal with a markdown changelog.** The release
  notes render as readable markdown (headings, bullets, links) in a centered dialog instead
  of a cramped plain-text corner panel.

### Fixed
- **Plugin views are themed in the desktop app** — the frozen sidecar now serves
  `/_ds/plugin-kit.{css,js}`, so plugin iframes (Notes, Docs) pick up the design system
  instead of rendering unstyled.

## [0.52.0] - 2026-06-19

### Changed
- **Desktop app catches up to v0.51.x.** A minor bump so the desktop build runs: the
  console **utility bar** (widgets + bottom panel) and the documentation overhaul now ship
  in the signed macOS / Windows / Linux binaries + in-app updater. No new runtime changes
  beyond v0.51.1.

## [0.51.1] - 2026-06-19

### Added
- **Utility bar in the console.** A compact bar with quick widgets on the left and layout
  controls on the right, plus a toggleable bottom panel. The **inbox** is now a utility-bar
  widget (on a reusable `UtilityWidget` primitive), and **plugins can contribute their own
  widgets** (a `utility:` manifest flag → an iframe dialog).

### Docs
- **Documentation overhaul.** Every Diátaxis section (Tutorials / Guides / Reference /
  Explanation) is now grouped by one consistent domain taxonomy in the sidebar and
  indexes, and the gaps are filled — guides for **ingestion** and **RAG tuning**, the
  **command palette (⌘K)**, **mid-turn steering**, an **Operator REST API** reference, a
  **Skills** reference, a "write your first skill" tutorial, a managed-MCP-server example,
  and a rewritten **operator-console** guide.

### Changed
- **The marketing changelog no longer shows empty releases.** Backfilled the recent empty
  entries, and the release tooling now omits a release that ships no notes instead of
  rendering a bare version + date.

## [0.51.0] - 2026-06-18

### Added
- **"Skills loaded" chip in chat.** The console shows which skills the agent auto-retrieved for a turn (hover a name for its description); toggle with `skills.announce`.
- **Author/edit skills in a modal dialog** in the console (instead of the in-panel form), plus a version badge + "built by protoLabs.studio" footer in the app drawer.

### Removed
- **Dropped the never-used `emit_skill` capture path.** The agent self-authors skills via `/distill`; the dead skill-v1 emission machinery is gone.

### Fixed
- **Desktop sidecar bundles `config/plugin-catalog.json`** so the plugin Discover directory works in the frozen app.

## [0.50.0] - 2026-06-18

### Added
- **Skills CRUD in the console.** Settings ▸ Workspace ▸ Skills now lets you
  **author, edit, and delete** skills — not just browse, delete, and promote.
  Operator-authored skills are persisted as portable `SKILL.md` files under a
  writable data-home root (`~/.protoagent/skills`, instance-scoped, via
  `infra.paths.user_skills_dir`) and seeded into the index on boot like any
  skill root, so they survive restarts, stay out of the repo working tree, and
  are exportable. Editing an agent-**learned** skill materializes it as a
  durable `SKILL.md` (curation = persistence); **bundled** examples and shared
  **commons** skills are read-only. New routes: `POST /api/playbooks` (create),
  `GET /api/playbooks/{id}` (full body), `PUT /api/playbooks/{id}` (edit); the
  list payload now tags each skill with `origin`/`editable`.

## [0.49.1] - 2026-06-18

### Fixed
- **Background-job dialog shows the full result**, not the truncated live preview.

## [0.49.0] - 2026-06-18

### Added
- **Native command-palette chat** recovered, a **`/effort` reasoning control** for chat turns, and a **Schedule rail** in the console.

## [0.48.0] - 2026-06-18

### Added
- **Command palette (⌘K).** Jump to any surface plus core actions, with chat and inline plugin views living inside the palette (ADR 0057).
- **Unified plugin manager.** Collapsed to **Discover** (an in-app official-plugin directory served from the host catalog) + **Installed** (per-plugin config folded into the rows, manifest-driven Test + guide link) — ADR 0059.
- **Always-on hamburger menu** replaces the header status-light / theme / settings cluster.

### Changed
- **Discord is no longer bundled** — it installs as a runtime external plugin in the frozen desktop app (ADR 0058).

### Fixed
- **Plugin git refs are validated before fetch**, and the dev server proxies `/_ds` so plugin-kit loads in plugin iframes.

### Docs
- **ADRs 0057 / 0058 / 0059** — command palette, runtime plugin install in the frozen app, and the unified plugin manager.

## [0.47.0] - 2026-06-18

### Removed
- **Google (Gmail + Calendar) and Slack are no longer bundled — they move to
  standalone external plugins.** The `google` plugin (`plugins/google/` +
  `mcp_servers/google/`, OAuth-gated managed MCP server) and the `slack`
  communication plugin (`plugins/slack/`, Socket Mode `ChatAdapter`) have been
  removed from core. They're re-published as installable external plugins from
  their own repos (tracked by GitHub issues), following the same pattern as the
  other standalone plugins — nothing about the integrations themselves changes,
  only where they live. The plugin contracts (ADR 0018/0019/0029) make this a
  no-core-edit lift-and-shift. The **Telegram** plugin (`plugins/telegram/`) stays
  in core as the reference `ChatAdapter` (ADR 0029). Existing `google:` / `slack:`
  config sections are simply unclaimed once the plugins are gone; install the
  external plugin to restore them. The `google` pip extra (`pip install -e .[google]`)
  and `requirements-google.txt` are gone — `requirements.txt` now installs core
  only (`-e .`); the dead "Connect Google" console affordance was removed from the
  Settings UI.

## [0.46.0] - 2026-06-17

### Added
- **In-app update notice with the changelog** in the desktop app — shows what changed instead of a generic prompt.

## [0.45.0] - 2026-06-17

### Added
- **Real chat streaming in the desktop app** — token-by-token output + tool cards over Tauri-relayed SSE.

### Fixed
- **Desktop in-app updater no longer 404s** — a release is marked "Latest" only once `latest.json` is published.

## [0.44.0] - 2026-06-17

### Fixed
- **Desktop updater public key now matches the signing key**, so in-app updates verify and install.

## [0.43.0] - 2026-06-17

### Added
- **Portfolio plugin (ADR 0055).** One PM agent dispatches work to, and tracks, several team-agents' project boards across repos over A2A — `portfolio_rollup` (bounded cross-board view), `portfolio_diff`/`portfolio_watch` (board deltas), and `portfolio_link`/`portfolio_plan` (cross-board dependency graph). Shipped as a standalone plugin.
- **Mid-turn steering.** Send a message while a turn is running and the agent folds it in at the next model call instead of stopping — with a ✕ to cancel a queued steer, and a Tier-2 control to cancel a single running subagent delegation.
- **Drag-to-reorder chat session tabs.**

### Changed
- **Setup wizard + forms rebuilt on the design system** (FormField / RadioCard, token cleanup).
- **Instance-scoped agents resolve their installed-plugin config correctly**, and idle ACP coding-agent runtimes are evicted from the runtime pool.

### Docs
- **ADR 0056** — unified dockable-view model (tabs ↔ rails).

## [0.42.0] - 2026-06-17

### Added
- **ACP `forget_session` — start a coder fresh when its workdir was recreated.** A
  persisted ACP session (#970) lets a dispatch *reattach* a prior thread — right when
  the workdir keeps its contents across calls, wrong when the caller **recreates the
  workdir fresh per attempt** (the project-board loop's disposable git worktree): a
  resumed thread carries memory of a diff the wiped tree no longer has, so the coder
  thinks it's already done (→ no diff) or edits against stale assumptions.
  `coding_agent.forget_session(spec)` (+ `AcpAdapter.forget_session(delegate)`) evicts
  the client and deletes the persisted session id so the next dispatch is a clean
  `session/new` — keeping the coder's memory in step with the (empty) tree.
- **`dream` & `distill` — scheduled self-curation subagents (ADR 0054).** Two new
  subagents the agent can run on demand (`/dream`, `/distill`) or on a cadence via
  the existing scheduler (`schedule_task "/dream"` — no new scheduling code).
  `dream` runs a memory-consolidation pass: it folds durable, verified facts into
  long-term memory **and prunes** the stale, superseded, and duplicate ones (the
  other half of consolidation). `distill` mines recent activity for repeated
  manual workflows and packages them as reusable skills with a **hybrid** policy —
  auto-create only the high-confidence, clearly-missing ones; propose the rest as
  beads for review. Both run on scoped, mostly read-only tools — **no shell, no
  raw SQL** — so the consolidation pass can't corrupt anything. New tools:
  `recent_activity` (read-only digest of the Activity feed + telemetry rollup),
  `list_skills` (read-only skill inventory), `save_skill` (additive-only — refuses
  to overwrite; saved as a curator-managed `distilled` skill), and `forget_memory`
  (delete one memory chunk by id). `memory_list` now leads each row with its
  `#<id>` so a fact can be targeted for pruning. Inspired by MiMo-Code's
  dream/distill commands, adapted to protoAgent's stores + native scheduler.
- **New-user setup wizard, rebuilt around archetypes.** The first-run wizard is
  streamlined to **four steps — Welcome → Agent → Brain → Summary**. Welcome opens
  with a local-first / privacy intro; **Agent** combines identity (name + operator)
  with a **persona picked from archetype cards** (Basic / Project Manager / Custom +
  any installed bundle) that seed an editable SOUL; **Brain** is the model or
  coding-agent (ACP) runtime (selecting ACP hides the gateway form); **Summary**
  recaps what you configured. Picking a **bundle archetype installs its tools** —
  choosing "Project Manager" clones + enables pm-stack (board + browser + delegates)
  into the host on finish, so you get the persona *and* the tooling in one pass.
  Each archetype carries a base SOUL on `GET /api/archetypes`
  (`config/soul-presets/{base,project-manager}.md`; installed bundles declare theirs
  inline). The **Workspace** and **Tools** steps are gone — their fields were all
  sensible defaults a new user shouldn't have to reason about (blank project dir →
  the protoAgent dir, blank knowledge DB → the default location, top-K 5, all
  middleware on, 40 researcher turns), so they flow straight through on finish and
  stay tunable in Settings. The model step also **auto-populates the gateway model
  dropdown** on arrival when an API base is set, so the picker is ready without a
  manual "Probe" (bd-hbf).

### Fixed
- **ACP coding-agent client: a real coding turn died on its own output.** The
  client read the agent's stdout with asyncio's default **64 KB line limit**, but a
  single ACP JSON-RPC message routinely exceeds that (a tool result with a file's
  contents, a large diff, a resumed session's history) — past the limit
  `readline()` raises `LimitOverrunError`, which tore down the read loop and
  aborted the turn mid-build. Raised the per-line ceiling to 32 MB. Also made the
  read loop **resilient + diagnosable**: a single malformed `session/update` (or a
  callback raising) is now logged and skipped instead of killing the whole session,
  the loop logs *why* it ends (it was silent before — failures surfaced only as an
  opaque "agent exited"), and `content` extraction handles list-shaped blocks (not
  just a single dict), which also raised `AttributeError` and killed the turn.
  Found by dogfooding the project-board coding loop end-to-end.
- **`operator.project_dir` actually drives the workspace root.** The configured
  project directory was only folded into `operator.allowed_dirs` and never persisted,
  so the real beads/notes root (`_resolve_operator_project_root`) ignored it. It now
  persists as `operator.project_dir` and the resolver honors it
  (env > configured-and-exists > default). (bd-2mf)
- **Setup probe could 500 or hang the runtime step.** Listing gateway models caught
  `httpx.HTTPError` but not `httpx.InvalidURL`, so a malformed API base 500'd and
  locked the step (and the response body was read twice). Broadened the guard to a
  clean error, read the body once, and added client-side timeouts on Probe / Test
  connection so a slow gateway can't pin the step's busy state (which disables Next).
- **Out-of-graph subagent runs now see the lead's full tool set.** A subagent run
  outside the lead's `task` tool (slash `/<subagent>`, a scheduled turn, the
  console fan-out) built its tools without `inbox_store`/`beads_store`, so an
  allowlisted name like a subagent's `beads_create` silently degraded to "not a
  valid tool". The runner now mirrors the lead graph's set (stores from `STATE`,
  goal mode from config); a test asserts every subagent allowlist resolves.

## [0.41.0] - 2026-06-15

### Added
- **Knowledge search returns the RRF relevance score.** `/api/knowledge/search`
  results (on a hybrid store) now carry a `score` — the RRF fused relevance used
  to rank them — so consumers can show or threshold relevance instead of getting
  bare ordered rows. Null on the plain-FTS store / `list_chunks` (unranked). (#1043)
- **`wait` resumes now appear live in the chat tab (ADR 0053 Slice 2).** When a
  `wait` (or scheduled task) resumes server-side, the scheduler fires a fresh turn
  into the originating chat thread — but the browser only renders turns it
  streamed, so the resumed turn was invisible until the next message. The terminal
  hook now pushes a `chat.resumed` event for a scheduler-fired turn that lands in a
  chat session, and a `ChatResumeWatch` appends the resumed answer to that tab live
  (display-only; the backend still owns history). Closes bd-k02.

### Fixed
- **Inbox: a fired `now` item is now marked delivered.** A now-priority inbox
  item (e.g. an ADR 0050 background-completion notification) fires an Activity
  turn on arrival, but it was never marked delivered — so it lingered as pending
  forever and the next `check_inbox` re-surfaced (and could re-act on) a backlog
  of already-handled notifications. A successful fire now marks the item
  delivered; a failed fire stays pending so `check_inbox` remains its fallback.

### Added
- **The fallback-models setting picks from the gateway list.**
  `routing.fallback_models` was a plain newline textarea; it now renders as a list
  of model comboboxes (one row per model + a blank row to add), each backed by the
  gateway's live model list — so you order fallbacks by picking real aliases (or
  typing any). Completes the settings-model-picker pass (`model.name`,
  `aux_model`, `transcribe_model`, and now `fallback_models` all use the gateway).

### Fixed
- **Scheduler startup catch-up no longer logs scary tracebacks.** When the
  scheduler's catch-up fires an overdue job before Uvicorn is accepting
  connections, the POST to the agent's own `/a2a` is refused — an expected,
  self-healing condition (the poll loop retries next tick). It now logs a concise
  "agent not reachable yet; will retry" at INFO instead of an ERROR
  `fire exception` traceback.
- **The scheduler retries the jobs.db owner-lock instead of giving up.** If the
  owner-lock was briefly held when the scheduler started — common on a
  restart/redeploy where the previous process freed the port but is still
  draining an in-flight turn — it logged "owned by another live instance" and
  **never started**, so `wait` resumes (ADR 0053) and every scheduled task
  silently didn't fire until an unrelated config reload happened to re-init it. It
  now retries in the background (~15s) and starts polling the moment the lock
  frees, so a contended boot self-heals in seconds. (Found driving the live agent
  — a `wait` sat 16 min overdue after a restart.)
- **`set_goal` rejects an unknown verifier instead of creating an unsatisfiable
  goal.** The tool only checked the verifier *type*, so a non-existent `check`
  (e.g. `"manual"`) created a goal that could never pass — it spun toward the
  iteration cap and ended `unachievable`. It now validates `check` against the
  registered plugin verifiers up front and lists the available ones, so the agent
  picks a real verifier. (Found driving the live agent.)

### Added
- **Settings model fields offer the gateway's model list.** The auxiliary model
  (`routing.aux_model`) and transcription model (`knowledge.transcribe_model`)
  were free-text boxes; they now render as comboboxes backed by the gateway's
  live model list (a datalist of suggestions), matching the primary-model picker —
  while staying free-text so a blank value or an alias the gateway doesn't list
  still works. (`model.name` and `knowledge.embed_model` already used the list.)

### Changed
- **The settings schema is cached client-side.** `GET /api/settings/schema` does a
  gateway round-trip server-side (it embeds the live model list for the pickers)
  and is read by both the Settings surface and every chat tab's composer model
  picker — so it now has a 5-minute React Query `staleTime` instead of refetching
  (and re-hitting the gateway) on every mount/focus. A settings save still
  invalidates it, so values stay fresh on change.
- **Per-tab model selection.** Each chat tab can now talk to its own model,
  overriding the globally configured one. The composer's model dropdown is now a
  per-tab control (sourced from the gateway's live model list) — "Default" uses
  the configured model; any other choice is stored on that chat session and sent
  with every turn. Backend: the chosen model rides the turn as `state["model"]`
  and a new `ModelOverrideMiddleware` swaps the lead model for that turn (clients
  built via `create_llm` and cached per model), so sibling tabs stay on their own
  models. Wired through `/a2a` (message metadata), `/api/chat` (a `model` field),
  and the OpenAI-compatible `/v1/chat/completions` (honors the request's `model`
  unless it's the agent's own advertised id). The cost-v1 DataPart already reports
  the model that actually ran, so per-tab routing is visible per turn.
- **One-call goal-driven recurring loop (`graph.sdk.start_goal_loop` / `stop_goal_loop`).**
  Wires the OODA / self-improving pattern — *run a tick every N toward a goal until its
  verifier passes* — in a single call, instead of a plugin hand-stitching the goal controller
  (set a monitor goal, ADR 0028/0030) + the scheduler (a recurring prompt, ADR 0003/0053).
  Sets a monitor goal verified by a plugin verifier and schedules the tick **into the goal's
  own session** (`context_id`), so it drives the right goal; `every` accepts a 5-field cron or
  a duration shorthand (`"15m"` / `"2h"` / `"1d"`); rolls the goal back if scheduling fails;
  `stop_goal_loop` clears the goal + cancels the tick (e.g. from an `on_achieved` hook).
  Generalizes the wiring the spacetraders `manage-the-fleet` skill described in prose (#1026).
- **Plugin telemetry + agent decision-log kit (`graph/telemetry.py`, `from graph.sdk import
  DecisionLog, telemetry, render_html`).** The observability surface an unattended/agentic
  plugin needs: `DecisionLog` (a capped audit trail of what the agent changed, and why),
  `telemetry(...)` (the standard envelope — status / metrics / hints / decisions / sections),
  and `render_html(...)` (a self-contained, `--pl-*`-token-themed HTML panel — with fallbacks,
  so it drops into any plugin console view without a specific stylesheet). All values escaped.
  Generalizes the spacetraders `_DECISIONS` ring buffer + `st_report` envelope + dashboard
  decision-log panel. Pure stdlib, host-free (#1027).
- **Runtime knobs + presets control surface (`graph/knobs.py`, `from graph.sdk import Knobs,
  make_knob_tools`).** A reusable, bounded, reversible control surface an LLM strategist can
  steer a deterministic plugin engine with: declare typed knobs once (`define`, with `lo`/`hi`
  clamps + `choices`), read them live in the engine (`get`), and `set` them coerced + clamped
  + validated + logged; named **presets** apply a curated knob bundle as one move
  (`apply_preset`, non-cumulative); a change log records every tune/preset. `make_knob_tools`
  auto-generates the agent-facing `<prefix>_knobs` / `_tune` / `_preset` tools. Pure stdlib
  (host-free, directly unit-tested). Generalizes the spacetraders `_TUNABLE`/`set_knob`/
  strategy-preset surface (#1028).
- **Host-free plugin test harness (`graph/plugins/testkit.py`).** A self-contained
  (stdlib-only) testkit that loads a plugin as a **package** — so a plugin's real engine
  modules (relative imports, module-level `@tool`, lazy `graph.*` host imports) can be
  unit-tested with no protoAgent running, not just `register()`. `load_plugin()` mirrors
  the runtime loader's `protoagent_plugin_<id>` convention; `install_host_stubs()` registers
  stand-ins for absent host modules (`graph.*` / `knowledge.*`) that are monkeypatchable and
  raise-loud-if-unpatched; `FakeRegistry` captures contributions. `scaffold_plugin(with_tests=True)`
  now **vendors** the testkit (`tests/_plugin_testkit.py`, verbatim) + a conftest that uses
  it, so new and standalone plugins get deep-module testing out of the box. Closes the gap
  hit building the spacetraders plugin, where `fleet.py`/`tools.py` couldn't be tested
  without extracting all logic into dependency-free modules (#1024).
- **Supervised background-task helper (`graph/supervisor.py`, `from graph.sdk import supervise`).**
  A reusable, watchdog-backed lifecycle for a plugin's long-running background engine: run a
  unit of work back-to-back, and a watchdog that **re-kicks** a crash, **restarts** a stall
  (frozen `progress` + a confirming `stall_check`), and **recovers** a known fault via an
  `on_crash` hook — so the loop survives unattended. The plugin supplies only the work + the
  predicates; the Supervisor owns create/cancel/re-kick/restart/heartbeat and a `status()`
  dict. Pure asyncio (host-free, directly unit-tested). Generalizes the ~150 lines of
  task/watchdog machinery the spacetraders fleet engine hand-rolled (#1025).

### Fixed
- **The Tools tab shows exactly what the agent can call.** `/api/tools` re-derived
  its inventory from `get_all_tools` (the shared lead+subagent base) + plugins +
  mcp, a *separate* assembly from what `create_agent_graph` actually binds — so it
  drifted both ways: it advertised `set_goal` while the model couldn't call it
  (bd-2aa) and it hid `task`/`task_batch`, the filesystem tools, `execute_code`,
  and the deferred search tool that the model *can* call (bd-67j). Now
  `create_agent_graph` stamps its final tool set on the compiled graph and the
  Tools tab reads that — one source of truth, no drift in either direction.
- **Slash-command palette can't drift from the dispatcher.** The chat dispatcher
  and the `/api/chat/commands` palette each encoded the `workflow > subagent >
  skill` precedence (and the shadowed-skill rule) separately. Both now resolve
  through one shared `_slash_kind` / `resolve_slash_commands` in `server.chat`, so
  what the palette lists always matches what actually runs.
- **Background subagent results are delivered back to the chat that started them.**
  A `task(run_in_background=True)` (ADR 0050) captured its `origin_session` from
  the tracing contextvar, which reads empty inside a tool body — so the job ran
  detached with no originating session and its result could never drain back to
  the spawning chat (the agent was told "you'll be notified" and never was). It
  now reads the session from injected graph state, so the completion notification
  lands on the originating conversation's next turn as designed. (Same root cause
  as the `wait`/`set_goal` fixes; third caller, now closed.)
- **Non-streaming chat no longer returns a silent empty `200`.** A turn that ends
  at an `ask_human` interrupt, after a `wait` yield, or scratch-only used to give
  `/api/chat` and the OpenAI-compatible `/v1/chat/completions` a blank assistant
  message — the streaming/A2A path handled all three but `_chat_langgraph` never
  got the same hardening. It now surfaces the `ask_human` question, runs the
  dropped-scratch kicker retry, and falls back to the last tool result (e.g. a
  `wait` "Yielding…" confirmation) so callers always get a signal. The two
  interrupt-detection sites are now one shared helper so they can't drift again.
- **First-party `web-research` skill is reachable again.** Its slash token was
  `research`, which collides with the deep-research *workflow* — workflows win
  dispatch and hide the skill from the command palette, so a shipped user-facing
  skill could never be invoked. Renamed to `/web-research`; the command builder
  now logs a one-time warning when any user-facing skill's slash token is shadowed
  by a workflow/subagent, so this can't happen silently again.
- **`set_goal` is now actually bound to the agent.** The tool (ADR 0028 — the
  agent owns a plugin-verified goal) was advertised in the Tools tab / `/api/tools`
  but never reached the model: `create_agent_graph` called `get_all_tools` without
  threading `goal_enabled`, so it defaulted off and `set_goal` was silently
  dropped from the bound toolset (calling it errored `"set_goal is not a valid
  tool"`). The `/goal` chat control message kept working — it's parsed before the
  graph — which masked the gap. The agent can now self-set a goal during
  autonomous/fleet/autopilot runs, not just when a human types `/goal`.
- **`wait`'s same-session resume now works (ADR 0053).** A `wait` issued in a chat
  was supposed to resume in *that* chat's thread with history intact, but the
  resume fired into the Activity thread instead: the tool read the originating
  session from `tracing.current_session_id()`, which is reliably set for
  middleware but reads **empty inside a tool body** under LangGraph — so the
  job's `context_id` was never stamped. Root cause: `create_agent` ran on the
  default messages-only state, so the declared `ProtoAgentState` (with
  `session_id`) was never wired in and the per-turn `session_id` was dropped.
  Fixed by passing `state_schema=ProtoAgentState` to `create_agent` and reading
  the session from injected graph state (`InjectedState`) in `wait` and `set_goal`,
  with the contextvar kept only as an off-graph fallback. (Both found by driving a
  running agent over the API; regression tests now drive a real graph turn rather
  than monkeypatching the broken function.)

## [0.40.0] - 2026-06-14

### Fixed
- **Left panel no longer springs back to ~50% when resized smaller.** The DS
  AppShell's single divider made `maxRightWidth` (720) double as a floor on the
  left column, so on a wide window the left couldn't drag below ~50% and snapped
  back. Fixed at source in the design system (`@protolabsai/ui` 0.34→0.35: a user
  drag/keyboard resize now respects only the column mins, so the left shrinks to
  `minLeftWidth` while `maxRightWidth` still caps default/reopen widths). Console
  bumped to 0.35; a `layout` e2e guards the left shrinking past the old floor.

### Added
- **Opt-in JSON logging (`LOG_FORMAT=json`)** — set it to emit one JSON object
  per log line (`ts`/`level`/`logger`/`message`, plus the exception traceback and
  any `extra=` fields) so aggregators (Loki, CloudWatch, Datadog) can index logs
  without a grok pattern. Default keeps the human-readable stdlib format; level
  (`LOG_LEVEL`) and the stderr stream are unchanged either way. (#876)
- **Deploy guide: backup/restore + shutdown semantics.** `docs/guides/deploy.md`
  gains an Operations section — how to back up the data dir without corrupting the
  WAL-mode SQLite stores (cold stop-and-tar or hot `.backup`), how to restore, and
  what `SIGTERM` does to an in-flight turn (cancelled-but-reconciled; 5s graceful
  drain). (#876)
- **`wait` resumes in the same conversation** (ADR 0053). When the agent calls
  `wait` inside a chat, the scheduled resume now fires back into **that chat's
  thread** instead of the Activity thread — so it wakes up with the conversation
  history intact and continues where it left off. The originating session is read
  from the same per-turn contextvar the background-subagent path uses; the
  scheduler `Job` gained a lazily-migrated `context_id` column (existing schedules
  keep working). Plain scheduled jobs still land in the Activity thread. (Live UI
  surfacing of the resumed turn in the chat tab is a tracked follow-up.)

### Fixed
- **Background-agent notifications render legibly again.** After the DS
  message-thread adoption, `role:"system"` chat messages — which in practice are
  background-agent completion reports (ADR 0050): a lede plus a full markdown body
  with tables/lists — were picking up the design-system's *terse one-line system*
  styling (centered text in a 100px-rounded pill), turning a report into an
  unreadable rounded blob. They now render as a left-aligned, readable inset card
  with a subtle left accent (still visually distinct as system/automation output).

### Added
- **`wait` tool — yield instead of busy-polling** (ADR 0053). When the agent is
  waiting for something to finish (a ship to arrive, a build, a cooldown, an ETA a
  tool reported), it can call `wait(seconds, then=…)` to **end the turn** and be
  re-triggered later by the scheduler with `then` as its instruction — instead of
  calling a status tool in a loop, which burned the entire 200-step recursion
  budget in one turn (the cause of the `GRAPH_RECURSION_LIMIT` crash some
  long-running tasks hit). A new `WaitYieldMiddleware` makes the turn end
  deterministically once `wait` runs; it's a no-op on every turn that didn't call
  `wait`. Lead-agent-only (needs the scheduler). Resumes run in the durable
  Activity thread, so long-horizon "do X, wait, do Y" work proceeds without
  spinning.
- **Paste images + large text as attachments** — pasting an image (a screenshot, even when
  the browser exposes it only via clipboard `items`) now adds it as an attachment, and
  pasting text over a threshold (~1500 chars or ~20 lines) becomes a removable attachment
  pill — routed through the same tiering as a dropped file (inline / RAG-indexed) — instead
  of flooding the input field. Short pastes still go straight into the field. Drag-drop uses
  the same image-aware collection.
- **File-only chat send** — you can now send a message with an attachment and no typed
  text (attach an image or doc and hit send with an empty field — e.g. "describe this").
  The composer's send gate enables on text **or** a ready attachment, matching the DS
  PromptInput (`@protolabsai/ui` bumped to 0.34 for the attachment-aware submit). The user
  bubble still shows just the 📎 attachment line, never a raw dump.
- **User-facing skills — trigger a skill with a slash command** (ADR 0052) — a SKILL.md can
  now opt in with `user_facing: true` (plus an optional `slash:` token), which makes it
  invokable as `/<slash> [args]` right from the chat composer's slash menu — alongside
  `/<workflow>` and `/<subagent>`. Unlike those, a skill command doesn't spawn a worker: it
  **injects the skill's procedure as a directive and runs a normal turn on the current
  thread**, so the lead agent follows the recipe with its full toolset and history intact
  (every streaming / HITL / goal invariant unchanged). The bundled **`web-research` skill is
  now `/research`**, and a new **`release-notes` skill (`/release-notes`)** turns a set of
  merged changes into grouped, audience-ready notes. Precedence on a shared token is
  `goal` > workflow > subagent > skill. Skills not flagged `user_facing` are unaffected (they
  keep surfacing only via implicit retrieval-injection). The skills FTS index migrates v3→v4
  on first boot (backup-and-rebuild from disk + persisted skills — no data loss).
- **Chat message toolbar — copy, fork, regenerate** (DS message-thread adoption) — the
  chat transcript now uses the design-system `Conversation`/`Message`/`MessageActions`
  components. Each settled assistant reply gets a hover toolbar: **Copy** the answer,
  **Fork from here** (opens a new chat tab seeded with the history up to that message —
  the original is untouched, so you can branch a conversation), and **Regenerate** (re-runs
  the last turn in place, no duplicate user bubble). `Conversation` brings smart auto-scroll
  (stays pinned while streaming, but won't yank you down while you read back; a jump-to-latest
  button appears when scrolled up), replacing the hand-rolled message list. The streaming and
  self-heal invariants (#613/#615) are unchanged — this is a render-layer swap.
- **`scheduler.fired` event + orphaned push-config sweep** (ADR 0051 Slice 3 follow-ups) —
  a scheduled job dispatching now publishes `scheduler.fired` on the event bus (live
  visibility into cron/one-shot fires). And push-notification configs whose task no longer
  exists are now swept (at boot + on the periodic task-prune tick) — the SDK store has no
  TTL, so stale webhook configs previously persisted forever; their lifetime is now tied to
  the task.
- **Native vision in chat** — when the active model accepts images (`model.vision`; true for
  e.g. `protolabs/fast`, `protolabs/smart`, and `protolabs/reasoning`/deepseek-v4), an attached
  image is sent **straight to the model as a multimodal part** instead of through the extraction
  pipeline. The composer base64s the image into an A2A image part (proto `raw` + `mediaType`);
  the executor turns inbound image parts into an `image_url` content block on the `HumanMessage`
  so the model sees the picture directly. Off by default (`model.vision` Settings toggle);
  non-vision models keep routing images through the pipeline. Verified end-to-end against the
  live gateway (deepseek-v4 correctly read a test image).
- **A2A alignment polish + realtime cost/goal events** (ADR 0051 Slice 3) — fixed a real
  bug: the **delegate A2A client now sends `A2A-Version: 1.0`** (a missing header made a
  strict 1.0 peer reject the call with `-32009`). The agent card now advertises a
  `documentation_url` + `icon_url`. Two new event-bus topics expose more realtime info:
  **`turn.usage`** (per-turn cost/tokens, for a live spend HUD) and **`goal.iteration`**
  (the self-driving goal loop's per-continuation progress, not just achieved/failed).
- **Renderable chat components over A2A** (ADR 0051 Slice 2) — the agent can render
  structured data as a real inline widget instead of a markdown blob, via a new
  `show_component(component, props)` tool. It rides a typed `component-v1` DataPart on the A2A
  envelope (same contract as tool-call/HITL parts) and renders through a curated, data-only
  registry — **table**, **key-value/status**, and **timeline** — safe without a sandbox
  (free-form generated UI still uses the artifact iframe path). New widgets are a registry
  entry, not new transport.
- **Background jobs: realtime progress + stop/inspect controls** (ADR 0051) — a detached
  background subagent's tool-by-tool progress now streams to the console: the jobs dialog
  shows a live `⊷ web_search ✓ fetch_url …` feed per running job (a new executor progress
  hook → `background.progress` bus channel). Each running job has a **Stop** button — and the
  agent gets **`stop_task`** / **`task_output`** tools — backed by a *real* A2A `CancelTask`
  that genuinely cancels the running turn (correcting a stale belief that cancel was mark-only).
  A foreground `task` delegation can also **auto-background** when it overruns a time budget
  (`BACKGROUND_AUTO_S`, off by default), so a long inline subagent stops freezing the turn.
  Canceled turns now record telemetry instead of vanishing.
- **Reasoning display in chat** — the model's `<scratch_pad>` / provider `<think>` deliberation,
  previously stripped server-side and never shown, now streams to the console as a **collapsible
  "thinking" block** above the answer (DS `@protolabsai/ui/ai` `Reasoning`). It rides its own
  channel — a `reasoning-v1` DataPart on WORKING status frames (`stream_visible_reasoning`
  incrementally extracts scratch_pad/think; the executor emits it; the frontend accumulates it
  into `message.reasoning`) — so the **answer artifact is untouched** and plain A2A consumers
  ignore it. The block is open while the model is thinking and auto-collapses when the answer
  begins.
- **Background-jobs console widget** (ADR 0050, Phase 3) — a pill in the utility bar shows a
  spinner + count while background subagents run and an unread dot when they finish; clicking
  it opens a dialog listing each job's status, live elapsed time, and (for finished jobs) its
  result rendered as markdown. Hydrates from `GET /api/background` and tracks live off the
  `background.{started,completed}` events. (A live per-tool progress card in the transcript is
  a follow-up — it needs a `background.progress` channel.)
- **Chat file upload (composer UI).** The chat composer can now take attachments — an attach
  button (DS `PromptInput`), **paste-to-attach**, and **drag-and-drop** — across txt/md/html/
  pdf and audio/video. Each file is uploaded to the tiered attach endpoint on pick; small docs
  are inlined into the message and large docs are indexed for retrieval (a big document is
  never dumped into the turn). The attachment context is prepended to what the *model* receives
  while the chat bubble shows only the typed text + a 📎 file list. Files are session-scoped and
  cleaned up when the chat is deleted.
- **Background subagents wake the agent on completion** (ADR 0050, Phase 2) — when a
  background job finishes, the agent now **reacts to the result autonomously** instead of
  only learning on the spawning chat's next message: the completion fires a turn into the
  Activity thread (via a `now`-priority inbox item, storm-guarded), where the response
  surfaces live in the console's Activity feed. So a backgrounded strategist audit can
  finish and the agent acts on it on its own. On by default; `BACKGROUND_WAKE=0` opts out.
- **Chat attachments — tiered context (backend)** (ADR 0021). `POST /api/knowledge/attach`
  extracts a dropped file (the ingestion engine — txt/md/html/pdf, audio/video via STT) and
  **tiers it so a big document never gets dumped into the turn**: text at or under
  `knowledge.attach_inline_budget` (default 8000 chars) is inlined whole; a larger doc is
  ingested (chunked → contextually enriched → embedded) under a per-session namespace
  (`attach:<session>`) so the user's *question* retrieves only the relevant passages, with
  just a lede inlined as an anchor. The attachments are **session-scoped + ephemeral** —
  deleting the chat (`DELETE /api/chat/sessions/{id}`) now drops them via the new
  `KnowledgeStore.delete_by_namespace` (hybrid clears the side vector table too). The
  composer UI that drives this is the next PR.
- **Background subagents** (ADR 0050, Phase 1) — the `task` tool now takes
  `run_in_background: true`. A long, independent delegation (deep research, multi-step
  gathering) runs **detached** instead of blocking the chat turn: the tool returns
  immediately with a job id, the work runs as its own A2A turn, and its result is
  delivered back into the spawning session's **next** turn as a `<task-notification>`
  (exactly-once) — so the conversation stays live while the work runs, instead of freezing
  on a single multi-minute tool card. **And if the spawning chat is still open, the result
  is pushed into it live** — a `system` message + a toast the moment the job finishes
  (`background.started`/`background.completed` on the event bus), no need to send a message
  to see it. Jobs are tracked in a durable, instance-scoped registry (`background/jobs.db`),
  reconciled on restart, and listed by a read-only `GET /api/background`. Disable with
  `BACKGROUND_DISABLED=1`. (Autonomous idle-wake, a background-jobs panel, and
  `task_output`/`stop_task` control tools are the planned Phases 2–4.)
- **Smarter subagent delegation** (ADR 0050 follow-up) — the agent now reaches for its
  specialized subagents instead of grinding their work inline. The `task` tool's
  `subagent_type` is a schema **enum** of the live registry (plugin-contributed subagents
  included), so the model can't pass a name that doesn't exist and sees the full roster; the
  delegation guidance steers domain work (deep research, strategy, multi-step gathering) to
  the matching subagent and **defaults heavy/long delegations to the background** so a
  multi-minute subagent run (e.g. a strategic audit) no longer freezes the chat.
- **Audio & video ingestion** (ADR 0021, ingestion engine Phase 2) — drop an audio file
  (mp3/wav/m4a/flac/ogg/…) or a video (mp4/mov/mkv/webm/…) into the knowledge base and it's
  transcribed, then chunked + enriched + embedded like any other document. Transcription
  rides the gateway's OpenAI-compatible `/audio/transcriptions` endpoint
  (`knowledge.transcribe_model`, e.g. `whisper-1`) — same gateway + key as chat/embeddings,
  no local ASR model. Video has its audio track pulled by `ffmpeg` (a host binary) first;
  a missing `ffmpeg`, or a blank `transcribe_model`, returns a clear error rather than
  failing silently. Direct audio/video URLs work too. The console "Add source" drop-zone
  now accepts these formats.
- **Document ingestion engine** (ADR 0021) — add real documents to the knowledge base,
  not just typed facts. A new core `ingestion/` package turns a source into text and
  feeds it through `add_document` (chunk → contextual-enrich → embed), so a whole PDF or
  article becomes per-passage recall. Phase 1 formats (light, pure-Python): plain text,
  Markdown, HTML, PDF (`pypdf`), web URLs (fetched + readability-stripped via
  BeautifulSoup), and **YouTube** links (transcript via `youtube-transcript-api`). New
  `POST /api/knowledge/ingest` accepts a file upload, a URL, or pasted text (extraction +
  embedding run off the event loop) and returns the created chunk ids. Each extractor
  degrades cleanly — an optional dep that's missing raises a friendly error, a bad source
  never 500s. The Knowledge console gets an **"Add source"** affordance — drop a file or
  paste a web/YouTube URL — alongside the existing typed-fact entry. Audio/video (local
  ASR) is a deliberate Phase 2 (the gateway serves no transcription model).
- **Contextual enrichment on knowledge ingest** (ADR 0021 — Anthropic's Contextual
  Retrieval). When a document splits into chunks, an aux-LLM one-line context that
  situates each chunk in the *whole* document is prepended before it's embedded and
  FTS-indexed — so the chunk's vector and its keyword terms both carry document-level
  context they'd otherwise lack (lifts semantic **and** BM25 recall). Builds on the new
  `add_document` chunking: enriches only genuinely multi-chunk docs (a single chunk is
  the whole doc), costs one aux call per chunk at **ingest** (never on the query path),
  and degrades to the raw chunk on any gateway hiccup. Off by default — flip
  `knowledge.contextual_enrichment` (Settings▸Knowledge); the document text sent in the
  context prompt is capped by `knowledge.context_max_doc_chars`. Harvest ingest is now
  offloaded to a worker thread so the per-chunk LLM/embed work doesn't block the
  maintenance loop.
- **Document chunking on knowledge ingest** (ADR 0021). Large bodies — harvested
  conversation summaries and operator-pasted docs — are now split into coherent,
  overlapping passages before embedding, instead of collapsing into one diluted
  whole-document vector. Each passage gets its own embedding, so semantic recall can
  land on the span that actually answers a query. Splitting is hierarchical
  (paragraph → sentence → whitespace → hard window) so chunks end on natural
  boundaries; short content (facts/notes) passes through unchanged. New
  `KnowledgeStore.add_document()` funnels each piece through `add_chunk` (the
  reasoning-strip guard + per-piece embedding still apply); a plugin backend that
  only implements the ADR 0031 surface degrades to a single un-chunked write. Tunable
  via `knowledge.chunk_max_chars` / `knowledge.chunk_overlap_chars` (Settings▸Knowledge)
  and `knowledge.chunk_min_chars` (config). Measurable with the retrieval eval harness.
- **Retrieval-quality eval harness** (`evals/retrieval.py` + `evals/retrieval_gold.yaml`).
  Measures the knowledge store's retrieval in isolation — recall@k / hit-rate@k / MRR /
  nDCG@k over a labelled gold set, split by query mode (keyword vs paraphrase) — which
  the A2A side-effect suite never did. Reports the hybrid-vs-keyword recall lift and can
  sweep the `vector_k` / `rrf_k` knobs. Runs against the real gateway embedder or a
  deterministic offline bag-of-words embedder; metric math is pure + unit-tested. This is
  the regression guard + measurement tool for the next RAG steps (chunking, contextual
  enrichment, reranking).

### Changed
- **Host config settings regrouped** (ADR 0047 D8 follow-up, bd-2zb) — the box-runtime host
  knobs that were lumped under one "Fleet" section now read as three coherent groups in
  Settings ▸ Host / App ▸ Host config: **Network** (bind interface + workspace port base),
  **Discovery** (mDNS + the discovery port window), and **Keep-warm** (warm-agent cap +
  eviction grace). Grouping only — same fields, same host cascade, same save path.
- **Chat composer migrated to the design-system `PromptInput`** (`@protolabsai/ui/ai`, bumped
  0.30 → 0.33). The hand-rolled `<form>`/`<textarea>` is replaced by the DS composer, driven
  through the new host-extension seams added upstream (`inputRef`/`onKeyDown`/`overlay`): the
  slash-command menu renders in the `overlay` slot with the same ↑/↓/Enter/Tab/Esc nav, ⌘/Ctrl
  +Enter still inserts a newline, and the send button becomes a stop control while streaming.
  Behavior preserved; the composer now tracks DS chat styling and is ready for file attachments.
- **Batched embedding on document ingest** (ADR 0021). `add_document` now embeds all of a
  document's chunks in a **single** gateway request instead of one serial `_embed` call per
  chunk — a 26-chunk web article went from 26 embed round-trips to 1. Rows are written before
  the embed, so a batch failure still leaves FTS5-searchable chunks (and trips the same
  circuit breaker); single-chunk docs, embeddings-off, or an open breaker fall back to the
  per-chunk path. New `create_embed_batch_fn` + `HybridKnowledgeStore(embed_batch_fn=…)`.
- **Parallel contextual enrichment on ingest** (ADR 0021). The per-chunk enrichment aux-LLM
  calls — the dominant ingest cost for a large enriched doc — now run **concurrently** (bounded
  pool) instead of serially. The first chunk is probed serially so a gateway outage still
  disables enrichment after one call (no N concurrent failing requests); a per-chunk failure in
  the parallel batch degrades just that chunk to raw. Order is preserved. Together with batched
  embedding, a multi-chunk document's ingest is now a single embed request + a concurrent burst
  of enrich calls rather than 2N serial round-trips.
- **Semantic recall tuned + made tunable** (RAG bake-off findings from internal research).
  `knowledge.top_k` raised 5 → 10 and the recall preview 240 → 1000 chars (more
  answer-bearing context in-prompt at no retrieval cost). The hybrid-store knobs are now
  config + Settings▸Knowledge fields instead of hardcoded: `knowledge.vector_k` (RRF
  candidate pool), `knowledge.rrf_k` (fusion constant), `knowledge.min_score` (a relevance
  floor, default 0 = off), `knowledge.recall_preview_chars`, and the embed circuit-breaker
  threshold/cooldown — so retrieval can be tuned without editing the store. All defaults
  preserve today's behavior except the deliberate top-k and preview bumps.
- **Setup wizard slimmed to the essentials.** The Discord and Google steps are gone —
  both are managed in System → Settings (with their own Test/Connect actions), so the
  wizard no longer collects bot tokens or OAuth clients. Finishing setup now leaves any
  existing Discord/Google config untouched (the YAML write merges, never replaces).
- **GitHub Copilot is now selectable as the ACP runtime** in the setup wizard's coding-agent
  list (`acp:copilot` → `copilot --acp`), matching the Settings runtime options.

### Fixed
- **Chat composer focus polish.** The migrated DS composer showed a double focus ring (the
  app's global `textarea:focus-visible` outline leaked through the DS field's own reset by
  specificity) — now suppressed so only the container's single focus ring shows. Clicking
  anywhere in the prompt box (its padding or button bar, not just the textarea) now focuses
  the input.
- **Embedding circuit breaker clears on a passing connection test.** After repeated embed
  auth failures (e.g. an expired gateway key) the breaker latches open for
  `embed_breaker_cooldown_s` and serves keyword-only FTS5. Fixing the key via Settings
  already recovered instantly (the store is rebuilt with a fresh breaker), but an
  out-of-band fix — hand-edited `secrets.yaml`, an env var, or a gateway-side recovery —
  left recall degraded until the cooldown elapsed. A successful **Test connection** of the
  live key (`/api/config/test-model` with no form-local key) now clears the breaker
  immediately, so semantic recall resumes at once. New `HybridKnowledgeStore.reset_embed_breaker()`.
- **Knowledge embeddings default to `qwen3-embedding`.** The setup wizard hard-coded
  `nomic-embed-text`, which the protoLabs gateway doesn't serve — so semantic recall
  401'd on every embed and silently degraded to keyword-only. The wizard now writes
  `qwen3-embedding` (matching the code default), and the **Embedding model** field in
  Settings▸Knowledge is now a gateway-model **dropdown** (pick from what your gateway
  serves) instead of free text, so it can't be typo'd into a 401.

## [0.39.0] - 2026-06-13

### Added
- **Restart the server from the console** (#979). A gated `POST /api/restart` plus a
  "Restart server" button in Settings▸Plugins gracefully restart the process (clean
  shutdown, then re-exec) and the console reconnects on its own — no terminal `Ctrl-C`
  needed after a change that can't hot-load.

### Fixed
- **The left console panel can narrow to 200px** (#980) — dragging it narrower no
  longer snaps it back up to 280 (the AppShell `minLeftWidth` floor was the default).
- **OpenShell deploy path validated end-to-end** against OpenShell v0.0.59 (#891).

## [0.38.0] - 2026-06-13

### Added
- **ACP client: restart-surviving sessions + thought streaming** (#970). The shared
  `coding_agent` ACP client (which the `delegates` plugin and `project_board` loop both
  drive) gained the rest of the session lifecycle, so a coding thread no longer dies with
  its subprocess. On start it now persists the `sessionId` per launch signature and, when
  the agent advertises the `loadSession` capability, **`session/load`s the saved thread**
  (replay suppressed — a silent reattach) instead of always `session/new`-ing; a stale id
  falls back to a fresh session. `close()` sends a best-effort **`session/close`** before
  the SIGTERM (graceful, spec-aligned teardown). `initialize` now **honors the negotiated
  `protocolVersion`** — it closes the connection on an unsupported counter rather than
  warn-and-continue. And **`agent_thought_chunk`** reasoning is surfaced via a new
  `thought_callback` (falling back to the progress narration) instead of being dropped.
  All in `plugins/coding_agent/acp_client.py`; the delegates plugin and project_board
  inherit it with no changes.
- **Settings: a scalar multiline `text` field + conditional `depends_on` visibility**
  (#964, #963). Long string settings (a system prompt, a template, a blurb) get a new
  `text` field type that renders a textarea but saves exactly like `string` — no more
  editing a paragraph in a one-line input. And any settings field (core `Field` or a
  plugin's `settings:` spec) can declare `depends_on: {key, equals}` (or `{key, in: […]}`,
  or a bare `{key}` for "is truthy") so it only shows once a prerequisite is set — the
  "enable X → show X's options" pattern (e.g. the artifact plugin's *Ask system
  instruction* appears only when *Interactive artifacts* is on). Reactive to the in-form
  value; a plugin's short `depends_on.key` is resolved to its full dotted path at build.

### Fixed
- **SSRF: the model-probe and fleet-remote registration now run egress checks** (#871).
  `list_gateway_models` / `validate_model_connection` (reached via `/api/config/models`
  and `/api/config/test-model`) made a raw request to the operator-supplied `api_base`
  with no guard — `api_base=http://169.254.169.254/…` was semi-blind SSRF that even
  echoed the upstream body. They now run `egress.check_url` before the request (blocked
  hosts need `egress.allowed_hosts`) and no longer echo raw upstream bodies. Registering
  a fleet remote (`add_remote`) validates the URL too — `allow_private` keeps LAN /
  tailnet / co-located remotes working while link-local / cloud-metadata / reserved
  targets are always blocked. `egress.check_url` gains `allow_private` +
  `block_unresolvable` modes (defaults unchanged for existing callers).
- **A plugin's declared secret can no longer slip into the tracked config YAML when
  plugin discovery fails** (#877). `secret_paths()` swallowed any discovery error to an
  empty set, so a transient failure stopped recognizing a plugin's secret keys and
  `strip_secrets_from_doc` would write them into the main (exportable/forkable)
  `langgraph-config.yaml` in plaintext. It now logs the failure and **falls back to the
  last successfully-discovered set** (fail-safe, never empty). `_resolve_plugin_config`
  likewise logs instead of silently returning `{}`.

## [0.37.0] - 2026-06-13

### Added
- **`graph.sdk.complete()` — a bare LLM completion for plugins.** The ADR 0043
  consumption SDK exposed only `run_subagent` (a full tool-using subagent); added
  `complete(prompt, *, system=None, model_name=None)` — a single bare model call (no
  tools, no agent loop, no persona) through the gateway. The clean primitive for a
  plugin that just needs the model to answer a prompt; first consumer is the artifact
  plugin's interactive `window.protoArtifact.ask()` bridge.

### Changed
- **Settings is a vertical-nav + collapsible-groups layout now.** The two stacked
  horizontal tab strips (scope + up to 11 sections) overflowed and read as "intense"
  once the Workspace home grew. The section nav is now a **vertical rail** (the scope
  toggle pinned on top, sections listed down the side), and each section's field
  **groups are collapsible** (DS `Accordion`, 0.29) with a "N unsaved" badge on a
  collapsed group that has edits. Field rows stack to a single column when the
  in-rail settings column is narrow, and stay two-column in the wide topbar overlay
  (whose body is a flex column so the rail + panel fill its full height). The rail is
  the DS `SideNav` ([protoContent#225](https://github.com/protoLabsAI/protoContent/issues/225),
  shipped in `@protolabsai/ui` 0.30.0); the collapsible groups use the `Accordion`
  from 0.29.
- **Design system bumped to `@protolabsai/ui` 0.30.0** — adds `SideNav` (adopted by
  the settings rail above).

### Fixed
- **The topbar Settings overlay panel fills the full dialog height** — the rail +
  panel now stretch to the overlay's bottom instead of stopping at the content height.

## [0.36.0] - 2026-06-13

### Changed
- **The committed `plugins.lock` now ships empty.** A fresh clone previously
  inherited the upstream developer's local installs (artifact, doom) as
  "missing / not enabled" rows in the Plugins panel. Upstream starts with no
  third-party plugins; your installs append to the lock, and forks/deployments
  commit theirs for reproducible checkouts (ADR 0027 unchanged).

### Added
- **One-click plugin sync from the console.** On a fresh checkout (or restored data
  dir) `plugins.lock` lists plugins whose gitignored code isn't on disk; the Plugins
  panel said "missing — run sync" but sync was CLI-only — a dead end for a new user.
  The panel now shows a banner with a **Sync plugins** button (new
  `POST /api/plugins/sync`) that re-clones every locked plugin at its pinned commit;
  anything already in `plugins.enabled` hot-reloads live. Fetch ≠ enable still
  holds — syncing never turns plugins on.
- **Knowledge base CRUD from the console.** The Knowledge → Store view was
  read-only; now the operator can curate it: **add** an entry (+ button — heading,
  domain, content), **edit** a chunk in place, and **delete** one (with confirm).
  Backed by `POST/PUT/DELETE /api/knowledge/chunks[/{id}]` on the ADR 0031 backend
  protocol — edit adds the new revision *before* deleting the old row (a failed
  save can't lose the original), and a hybrid store re-embeds the new content.
  Operator-added entries carry `source: console / source_type: operator`.

### Changed
- **Harvest-on-delete is now opt-in.** Deleting a chat tab silently summarized the
  conversation into the knowledge base first; the delete dialog now has a
  **"Harvest into the knowledge base first"** checkbox (off by default — deleting a
  chat shouldn't copy it into searchable memory unless you ask). The API gained
  `DELETE /api/chat/sessions/{id}?harvest=true|false` (default false); the TTL
  prune sweep keeps its config-driven `checkpoint_harvest_enabled` default.

### Fixed
- **Drag-and-drop rail positions for plugin views now survive a reload.** On boot
  the rail reconciler ran once against the not-yet-loaded plugin list, pruned every
  persisted `plugin:` entry as "uninstalled", then re-appended the loaded views at
  their manifest `placement` — silently wiping the operator's arrangement on every
  reload and making the declared rail look like it always overrode the saved one.
  The reconciler now waits for the runtime status to resolve (unknown ≠ empty); a
  view's `placement` is only the default for its first appearance.
- **A render error no longer white-screens the console** (#872): a root error
  boundary around the app shows a full-page recovery card — Reload, plus "Reset
  chat data & reload" which clears `protoagent.chat.sessions*` (the known way a
  corrupt blob bricks render) while keeping layout/theme/auth token. Fork-registered
  chat surfaces (`src/ext`) are boundary-wrapped too, so a throw stays contained in
  the slot. Persisted chat sessions are now shape-validated on load — invalid
  members are dropped (the rest survive) instead of throwing later in render.
- **The documented kit-loading pattern for plugin views was broken** — `plugin-kit.js`
  is an ES module, so the classic `<script src>` the docs and `chat_example` taught
  threw `Unexpected token 'export'` and the page's kit logic never ran. The notes
  editor, `chat_example`, and both plugin-view guides now load the kit via dynamic
  `import(base + "/_ds/plugin-kit.js")` (filed upstream as protoContent#224).

### Added
- **The console prompts for the operator token on 401** (#873): any unauthorized
  response — panel query, boot probe, or chat turn — opens an "Authentication
  required" dialog that saves the bearer to `protoagent.authToken` and refetches
  in place (no reload, no devtools). 401s no longer burn retries, and a token-gated
  first run shows the prompt instead of the BootGate's misleading "isn't responding".

### Changed
- **The Notes plugin editor adopts the DS plugin kit (rule 4)** — `plugin-kit.css`
  `--pl-*` tokens + `initPluginView` + slug-aware authed `apiFetch` replace its
  hand-rolled hex theme map, bespoke `protoagent:init` listener, and manual bearer
  headers; the editor now follows the operator's live theme (including the new
  OS-adaptive light presets). Notes plugin → 0.2.0.
- **Design system bumped `@protolabsai/ui` 0.26.2 → 0.29.0 (+ `@protolabsai/design` 0.5.1).**
  Brings the OS-adaptive light theme + 10 builtin theme presets (Theme panel picks them
  up automatically), two token fixes splash.css silently depended on (`--pl-space-5`,
  `--pl-font-weight-semibold`), and the new `@protolabsai/ui/ai` chat module (not yet
  adopted). The settings **Host / App | Workspace** home toggle and the MCP add-server
  **Form | Paste JSON** toggle now use the DS `Tabs variant="segmented"` pill control
  (our protoContent#218 request, shipped in 0.28) — retiring the stacked-tabs interim
  and the last hand-rolled `.segmented` CSS.

### Added
- **ADR 0049 — bundle pin lifecycle**: a bundle pin means *"last verified working"* —
  pin release **tags** (not raw SHAs), record `verified_against:` (core version), and
  let a **verify-and-bump CI loop** own the pin (install the pin set into a scratch
  agent → probe every declared console view → auto-PR tag bumps). Reference template
  with the rules baked in under `examples/bundles/template/` (manifest + verify/bump
  scripts + workflow); adopted by the pm-stack bundle. Motivated by the pm-stack
  incident: stale authoring-time pins shipped 404 Board/Browser panels to every
  agent spawned from the archetype.

### Fixed
- **Force re-install no longer claims a live hot-mount it can't deliver (#942).**
  Re-installing a plugin whose router is already mounted re-registers the router on
  reload, but FastAPI can't swap a mounted router in place — the fresh routes keep
  serving the OLD code until a restart, while `POST /api/plugins/install` answered
  `restart_recommended: false` (hardcoded). The install route now reads the live
  mount registry (which survives a disable, unlike plugin meta) and flags the
  restart honestly, and it purges the re-installed plugin's module subtree before
  the reload (parity with the update route) so a multi-file plugin's tools run the
  fresh checkout. The update route's restart heuristic also gained the
  mount-registry check for the disabled-but-still-mounted case.
- **Annotated-tag pins no longer report a permanent false "Update available".**
  `git ls-remote <url> <tag>` returns the *tag object* SHA for an annotated tag —
  never equal to the lock's commit SHA — so tag-pinned plugins (e.g. `artifact@v0.2.1`)
  showed "behind" forever and Update could never clear it. The check now also asks for
  the peeled `<tag>^{}` ref and compares commit-to-commit.

## [0.35.3] - 2026-06-12

### Changed
- **Identity panel: the "Saving writes SOUL.md…" helper + save status now sit
  ABOVE the SOUL.md editor** (next to the Save button), instead of trailing under
  it — so the editor runs to the panel bottom without a footer of helper text.
- **`execute_code` is hidden and disabled in the desktop app**, and its Settings
  helper text was rewritten. It can't run in the packaged build (no standalone
  Python interpreter to spawn), so the frozen sidecar no longer offers the tool to
  the model *or* shows its toggle. The helper text also now states plainly that the
  subprocess is "isolation, not a true sandbox" (the old text oversold it). From
  source / Docker: unchanged, still off by default.

### Fixed
- **Desktop first-run: panels no longer flash "Load failed" and need a reload.**
  The cold-start retry policy only rode out HTTP 409/502 (a fleet member spawning),
  but the desktop sidecar's ~12s first-launch boot is a *connection-refused* network
  error (WKWebView reports it as `TypeError: Load failed`) with no HTTP status — so
  beads/notes/etc. fell through to the single-retry default, gave up before the
  sidecar bound its port, and stuck in the error fallback until manually reloaded.
  `isColdStart` now also treats a response-less fetch failure as "not up yet", so
  those panels stay in their loading state and resolve once the sidecar is ready.
- **macOS desktop: the brand again clears the native traffic lights.** A rename of
  the topbar element (`.topbar` → `.app-topbar`) left the title-bar inset rule on the
  old selector, so the 84px inset silently stopped applying and the window's
  traffic-light buttons crowded the logo. Pointed the `.is-tauri-mac` inset at
  `.app-topbar`.
- **`execute_code` no longer poisons a chat thread in the desktop app.** It spawns
  `sys.executable -u <script>`, but in the frozen build that's the server binary,
  not Python — so the spawn hung and the model's tool call never got a result,
  leaving a dangling `tool_calls` message that 400'd every later turn
  (`insufficient tool messages following tool_calls`). `run_code` now returns a
  clean error in a frozen build instead of the broken spawn.
- **The agent self-heals a chat thread left with a dangling tool_call.** Any turn
  that persists an assistant `tool_calls` message whose result never landed (a
  hung tool + a follow-up message, an interrupted/crashed turn, a dropped stream)
  used to 400 *every* later turn until the chat was deleted. A new
  `ToolCallRepairMiddleware` drops the unanswered tool_call from the history before
  each model call so the request is valid again — a no-op on a healthy history, so
  it never touches a normal turn.

## [0.35.2] - 2026-06-12

### Fixed
- **Switching agents in the desktop app no longer breaks the window.** The desktop
  build pinned Vite's `--base ./` (relative), so the bundled `index.html`
  referenced its JS/CSS relatively. At the root URL that's fine, but the fleet
  switcher navigates to a real path (`tauri://localhost/agent/<slug>/`), where
  `./assets/…` resolved to `/agent/<slug>/assets/…` — which doesn't exist, so
  Tauri's asset resolver fell back to `index.html` and the browser rejected it
  (`'text/html' is not a valid JavaScript MIME type`, `Did not parse stylesheet`).
  Switched the desktop build to an absolute `--base /` so assets always resolve
  from the protocol root regardless of route. (`apiBase()` already hard-targets
  `127.0.0.1:7870` in the Tauri context, and `agentHref` reads `BASE_URL`, so
  navigation + API are unaffected.)

### Added
- **The app version is shown in Settings ▸ Host / App ▸ Overview** (the runtime
  status already carries it — `0.35.1` etc.; the frozen desktop sidecar reports
  its bundled version per #894). Surfaced as a "Version" tile and in the panel
  subtitle, so there's an at-a-glance "about" for which build you're running.

## [0.35.1] - 2026-06-12

### Fixed
- **The desktop app's backend now actually starts (it was dead on arrival in
  v0.35.0).** The signed/notarized macOS build runs the PyInstaller server
  sidecar under the hardened runtime, which enables **library validation** — and
  the frozen onefile extracts and `dlopen()`s a bundled `Python.framework` signed
  with a different Team ID than our Developer ID. Library validation refused to
  map it (`code signature ... not valid for use in process: ... different Team
  IDs`), so the sidecar never started: the console loaded but every API call
  failed (no beads, no settings, the boot gate stuck on "…attention in
  Settings"). Added the sanctioned, notarization-permitted
  `com.apple.security.cs.disable-library-validation` entitlement so the embedded
  interpreter loads. CI missed it because the frozen-sidecar smoke runs on the
  **unsigned** binary (before bundling) — added a **post-sign smoke** that boots
  the signed sidecar from the bundled `.app`, so a signing-induced backend
  failure fails the build instead of shipping. `verify-macos-desktop.sh` now
  *requires* the entitlement (it previously forbade it).
- **Desktop release builds now write logs.** `tauri-plugin-log` was initialised
  only under `cfg!(debug_assertions)`, so release builds produced no log file —
  which is why the sidecar boot failure above left no trace on disk. Logging is
  now always on; `[sidecar]` stdout/stderr (including a boot crash) is captured
  to `~/Library/Logs/studio.protolabs.protoagent/`.

## [0.35.0] - 2026-06-12

### Added
- **Box-runtime knobs (bind interface, fleet ports, discovery, warm policy) are now
  Host-layer settings, not just scattered env vars** (ADR 0047 D8 — the cascade's
  final slice). `network.bind`, `fleet.port_base`, `fleet.discovery.port_min/port_max/mdns`,
  and `fleet.warm.max/grace_seconds` join `FIELDS` as `scope="host"`, so they cascade
  App → `host-config.yaml` → agent-leaf like every other host default and get a home in
  **Settings ▸ Host / App ▸ Host config** (with the inherited-from-Host / override / reset
  badges for free). Each pairs with an **env-var fallback** so existing `PROTOAGENT_HOST` /
  `PROTOAGENT_FLEET_MAX_WARM` / `PROTOAGENT_FLEET_WARM_GRACE` boxes keep working unchanged —
  precedence is **file > env > default** (a value set in the file/UI can't be silently
  shadowed by a leftover env var), and an explicit `--host` flag still wins over all of
  them. The host process's bind, the workspace port picker, fleet discovery (scan range +
  mDNS gate), and the warm-agent supervisor now read the resolved config instead of reading
  env at the call site; a CLI/no-config context falls back to env exactly as before.
- **An app update can no longer silently strand fleet members on the old binary**
  (version-coherence P2). Members are detached processes — one that survives a hub
  update (crashed hub, or the `PROTOAGENT_FLEET_KEEP_MEMBERS_ON_EXIT` opt-out) keeps
  running OLD code indefinitely, invisibly. Now: `start()` stamps the spawner's app
  version on the member record, the hub stamps each boot's version beside `fleet.json`
  and logs the transition (`reconcile_on_boot` — an in-app update, DMG swap, or
  `git pull` all land here), and a **live, self-clearing warning** rides the runtime
  status whenever a running local member's spawn version differs from the hub's —
  same posture as the co-location banner, clearing the moment the member is
  restarted. The Fleet panel's version-skew badge now covers local members too
  (it was remote-only), with a restart hint.

### Added
- **The desktop app updates itself in place.** tauri-plugin-updater wired into the shell:
  a silent check at launch (release builds) plus a tray "Check for Updates…" item; it polls
  `latest.json` on the GitHub Release, verifies the bundle's minisign signature against the
  org public key baked into `tauri.conf.json`, installs, and relaunches — agent data is
  untouched. CI: when the org `TAURI_SIGNING_PRIVATE_KEY` is present, every desktop leg
  emits signed updater bundles (`.app.tar.gz` / `-setup.nsis.zip` / `.AppImage.tar.gz`,
  v1-compatible shapes) and a fan-in job composes `latest.json` from all three platforms
  and uploads it *last*, so the manifest never points at missing assets. A release built
  without the key just ships without in-app update for that cycle. (`.deb` installs stay
  apt-managed — the updater handles AppImage only on Linux.)

### Fixed
- **Session memory now persists on non-container hosts (and stops writing to the drive
  root on Windows).** `MemoryMiddleware`'s default path was a literal `/sandbox/memory/` —
  on any machine without a `/sandbox` mount (local dev, the desktop sidecar) the create
  failed on read-only `/` and persistence was *silently skipped*, so agents had no
  cross-session continuity by default; on Windows the path resolved drive-relative and
  happily wrote to `\sandbox` at the drive root (caught by the desktop sidecar smoke).
  The default now routes through `data_home()` — `/sandbox/memory` in a container, else
  `~/.protoagent/memory`, instance-scoped as before — the same writable fallback every
  other store already used. `KnowledgeMiddleware.load_memory` drops its duplicate path
  literal and defers to the writer's resolved `MEMORY_PATH`, so reader and writer can't
  drift. `MEMORY_PATH` env override unchanged.
- **The console is an installable PWA (manifest-only — deliberately no service worker).**
  The pre-console-era `/manifest.json` was stale (`start_url: "/"`, SVG-only icons) and
  never linked from the React console; `static/sw.js` was served but never registered.
  Now: the manifest targets `/app/` (id/start_url/scope — fleet slug windows included),
  gains PNG icons (192/512 + apple-touch-icon, derived from the desktop app icon), and
  the console links it (plus `theme-color`). Install-to-dock/homescreen works in
  Chrome/Edge/Safari with **zero service-worker risk**: no SSE interception on `/a2a`,
  no stale-asset caching (the version-coherence class), no WKWebView SW flakiness — the
  link 404s inertly inside the Tauri webview. `sw.js` stays unregistered.

### Fixed
- **A secret saved for an installed-but-DISABLED plugin now routes to `secrets.yaml`,
  not the plaintext config.** Secret routing (`secret_paths`) and the config-redaction
  path keyed off *enabled* plugins only — so a secret for a plugin that's currently off
  (or being configured before enable) wasn't recognized as a secret: it would be written
  to the live `langgraph-config.yaml` in plaintext (gitignored, so never committed — but
  the wrong file: configs get exported / backed up / tracked in a fork) and echoed back
  unredacted to the Settings API. Both paths now cover ALL INSTALLED plugins
  (`installed_plugin_config_schemas`); the settings UI stays enabled-only. Found by a
  plugin-lifecycle audit.

### Fixed
- **The devkit's "edit then `reload_plugins`" loop now picks up edits to EVERY file, and
  reports when a plugin failed to load.** Two reliability gaps in the agent's make-it-live-
  and-test loop (found by a lifecycle audit): (1) the hot-reload re-exec'd only a plugin's
  `__init__.py`, so an edit to a sibling module (`from .impl import …`) silently served STALE
  code until a process restart — the loader now purges the plugin's whole `sys.modules`
  subtree before re-exec, on **every** reload path (not just `update`). (2) `enable_plugin` /
  `reload_plugins` / scaffold's live-enable reported "loaded live" whenever the config reload
  succeeded — but a plugin whose `register()` raises is *skipped* (best-effort load), so the
  agent was told a no-op worked; they now read the real per-plugin load status and surface
  "FAILED to load: <error>" so you fix-and-reload instead of testing nothing.
### Added
- **Settings are reorganized around *scope* — a two-home shell + contextual quick-settings (ADR 0048).**
  The Settings surface is now **two scope homes**, replacing the flat category tabs and the separate
  Agent rail surface: **🖥 Host / App** (box-shared: Overview · Host config · Fleet · Telemetry ·
  Commons) and **🧩 Workspace** (the focused agent's full makeup — Identity · Settings · Tools · MCP ·
  Subagents · Skills · Middleware · Memory · System · Theme · Plugins). Scope is the primary axis
  (`settingsTab` → `settingsScope` + `settingsSection`, persist v3). The standalone **Agent** rail
  surface is gone (folded into Workspace) and Knowledge is now store-only (its Memory settings moved to
  Workspace ▸ Memory). Alongside this one-stop-shop, a reusable **`QuickSetting`** primitive puts a
  gear-icon → dialog *contextual* shortcut wherever a setting is relevant — editing the same fields via
  the same cascade-aware `/api/settings` write path (host-scoped fields route to the host layer). The
  **topbar gear** opens the whole one-stop-shop as an overlay from anywhere, and contextual quick-set
  gears sit where they're relevant: **model tuning** by the agent name, **appearance**, **telemetry**
  policy (on the Telemetry view), **recall** (on Knowledge), and **skill-sharing mode** (on Skills).
  Part of #916.
- **The shared-skill commons is now legible in the console (ADR 0041 / 0048).** The
  layered skill tier ("shared brain, private hands" — read commons ∪ private, write
  private) shipped at the data layer but was invisible: the Skills surface couldn't tell
  a private skill from a commons one, the one curated action (`promote` a private skill
  into the box-shared commons) had no API route or button, and the skill-sharing mode was
  YAML-only. Now: a **tier badge** (commons / private) on each skill, a **Promote** action
  on private skills (`POST /api/playbooks/{id}/promote` over `LayeredSkillsIndex.promote`),
  and two new settings fields — `skills.scope` (`scoped` · `shared` · `layered`, per-agent)
  and `commons.path` (the box-shared commons location, host-scoped). Surfacing the second
  of protoAgent's two inheritance systems (the skill **union**, alongside the ADR 0047
  settings **override** cascade). Part of the settings-IA reorg (#916).
- **macOS desktop releases are now verified pristine — and the DMG itself is notarized.**
  Tauri notarizes the `.app` inside the bundle, but the DMG *container* shipped without its
  own ticket; the workflow now runs `notarytool submit` + `stapler staple` on the DMG, then
  `scripts/verify-macos-desktop.sh` mounts the artifact that actually ships and asserts:
  structure (main binary + bundled sidecar, both arm64), `codesign --verify --deep --strict`,
  Developer ID authority, the entitlement set is *exactly* what `entitlements.plist` declares
  (and nothing broader), Gatekeeper assessment, and stapled tickets on both the app and the
  DMG. Unsigned dispatch builds run the structure checks and skip the signing battery.
  (Ported from the ORBIS release pipeline.)
- **Desktop builds for Linux and Windows.** `desktop-build.yml` now fans out a
  three-leg matrix: the macOS `.dmg` (signed + notarized, as before), Linux
  `.AppImage` + `.deb` (x86_64, built on ubuntu-22.04 for the broadest glibc reach a
  hosted runner offers), and a Windows NSIS `-setup.exe` (x86_64, unsigned for now —
  SmartScreen will prompt until a Windows signing identity is added). Every leg also
  **smoke-tests the actual frozen sidecar before bundling** (`scripts/live_smoke.py
  --bin` boots the PyInstaller binary with no repo on `PYTHONPATH` and drives a real
  A2A turn — per-platform under-collection now fails CI, not the first user), and the
  real release version is **stamped into `tauri.conf.json` at build time** so the
  installer/app metadata stops claiming the in-tree placeholder.

### Changed
- **A configured plugin/model secret now shows a clear "set" badge in Settings.** Secrets
  never echo their value, so a saved key looked identical to an empty one ("did it save?").
  The generic Settings surface now renders a "set" badge next to a configured secret field
  (matching the Delegates panel) — a saved token is glanceable, not just a faint placeholder.
  (First slice of the plugin/bundle lifecycle tightening — single-agent.)

### Changed
- **Adopt `@protolabsai/ui@0.26.2`** — picks up the AppShell iframe-drag fix
  (protoContent #212 + #214): resizing a panel that hosts a plugin iframe now tracks
  smoothly and collapses on release, via `.pl-appshell-frame--dragging iframe { pointer-events:
  none }` (the window keeps the gesture over the iframe; the col-resize cursor is inherited by
  the column behind it). 0.26.1 also tried a full-window drag overlay, but it covered the
  divider handle and broke double-click-to-collapse — caught by our layout e2e — so 0.26.2
  dropped it. Removes the app-side interim guard from #903; the design system now owns it.

### Fixed
- **A fleet member's plugin secret (e.g. a SpaceTraders token) now actually saves +
  reads back, instead of showing "unset" after you enter it.** A member is launched with
  both `PROTOAGENT_CONFIG_DIR=<workspace>` and `PROTOAGENT_INSTANCE=<id>`, and config_io
  applied `scope_leaf()` on top of the already-per-member config dir — double-nesting the
  config/secrets to `<ws>/<id>/secrets.yaml`. The secret persisted there (securely — in
  `secrets.yaml`, mode 0600, never the tracked config), but the member's plugin-config
  resolver looks for the plugin at `<ws>/plugins`, so it never found the section to merge
  the secret into → the Settings field reported `is_set: false`. The config-dir-relative
  paths (config / secrets / setup-marker) now skip `scope_leaf` when `PROTOAGENT_CONFIG_DIR`
  is explicit (the dir is already the isolated leaf), and a one-time self-heal drops the
  orphaned `<ws>/<id>/` dir on the next member restart (re-enter the token once). Regression
  tests cover the scope helper, the plugin-secret round-trip, and the self-heal.
- **Switching to a not-yet-running fleet agent no longer flashes errors in its panels.**
  A cold agent answers 409 (the member is still spawning) then 502 (booting, not bound yet)
  for a few seconds until it's up — but only the boot probe retried through that window, so
  the other panels (beads/theme/…) gave up after one retry and surfaced a "failed" flash
  mid-boot. `request()` now throws a typed `ApiError` (carrying the status), and the
  QueryClient default rides out cold-start codes (409/502) — panels stay in their loading
  state until the agent answers, then fill in. A genuinely-down agent still surfaces via the
  shell's boot-gate "isn't responding". (ADR 0042 cold-start polish.)
- **A declined or failed tool now shows the red X on its card, not a green "done".**
  A denied `run_command` returned a normal string, so the card closed *green* with the
  decline text — the opposite of how a denial should read. `run_command` now raises on
  deny (and on execution error), so the ToolNode stamps the result `status="error"`; that
  flows through as a `phase="failed"` tool-call DataPart and the card renders the X (the
  protocol already supported the failed phase — nothing new on the wire). Enforcement-
  blocked tools get the same treatment. With the card already sitting yellow/running
  during the approval pause and turning green on approve, a gated action now reads exactly
  as intended: **yellow while you decide → green on approve, red X on deny** — no extra
  "approved" bubble (#904).
- **Approving a gated action no longer dumps an "approved" bubble into the chat.** When
  the agent gated a command behind an Approve/Deny prompt, the resume posted the literal
  word `approved`/`denied` as a *user message* — noise that cluttered the transcript and
  broke the read of the tool flow. An approval resume is now silent: the agent still gets
  the decision, but the outcome belongs to the tool card (running → done on approve), not a
  redundant bubble. Real input — `request_user_input` forms and `ask_human` questions —
  still shows the answer, since that *is* conversation.
- **Resizing panels felt sloppy and "wouldn't close right" over plugin views.** The DS
  AppShell's divider drag tracks the pointer on `window` listeners, so a plugin-view
  **iframe** captured `pointermove`/`pointerup` the instant the pointer crossed it — the
  resize stuttered and the pointer-up that commits a collapse was lost. Interim app-CSS
  guard (`.pl-appshell-frame--dragging iframe { pointer-events: none }`) restores smooth
  tracking + collapse now; the proper DS fix (a drag overlay that also carries the resize
  cursor over iframes) is protoContent#212 and lands when we bump `@protolabsai/ui`. The
  DS stories felt smooth only because they used plain `<div>`s, never iframes.
- **Swapping between fleet agents wiped the chat view.** The tenant guard (which
  clears persisted chat when the backend behind an origin re-keys) was reading the
  *focused agent's* `instance_uid` (slug-routed runtime status). Every fleet swap
  changes the focused agent → its uid changes → the guard fired and cleared **all**
  slugs' chat. It now keys on the **hub's** uid (a host-pinned runtime read, never
  slug-routed) — the hub is the actual tenant of the origin and is stable across
  swaps, so switching agents keeps each agent's chat. The guard still fires on a real
  re-key (a fork booting on the hub's old port).
- **The fleet proxy now forwards WebSocket upgrades (#883).** The hub's
  `/agents/<slug>/*` reverse proxy was HTTP-only (it even stripped `Upgrade`/
  `Connection`), so a fleet member's plugin that opens a live WS — `agent_browser`'s
  viewport/feed, say — loaded its panel over HTTP but its socket showed
  "Disconnected" behind the hub. Added a WS route (`proxy.forward_ws`) that resolves
  the slug → member, opens a client WS (carrying the bearer + subprotocols), and
  pumps frames both ways until either side closes. Live plugin sockets now traverse
  the hub like HTTP does.

### Changed
- **Installing a plugin from the console now auto-enables + runs it** (ADR 0027,
  trust-by-default). Previously install ≠ enable: you installed, then had to find the
  Enable toggle (a buried, easy-to-miss step — and a bundle had no single toggle at
  all). Now `POST /api/plugins/install` adds the plugin (or every bundle member) to
  `plugins.enabled` and hot-reloads, so its tools, console views and background
  surfaces come up live with no separate step and no restart (the router hot-mounts,
  #822). A failed enable-reload is surfaced (`enable_error`) without failing the
  install. The CLI `plugin install` stays fetch-only (reproducible/scripted setups);
  set `PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE=1` to make the console match it. (A
  one-time "this runs code" confirm for unofficial sources, with "don't show again,"
  lands next.)
- **Grouped the loose root-level modules into packages** (pure restructure — no
  behavior change). The 13 modules that sat at the repo root are now cohesive
  packages: **`a2a_impl/`** (`auth`/`executor`/`stores` — named to avoid shadowing
  the a2a-sdk's top-level `a2a`), **`observability/`** (`metrics`/`tracing`/
  `telemetry_store`/`pricing`/`audit`), **`security/`** (`egress`/`policy`), and
  **`infra/`** (`paths`/`cache`/`autostart`). Imports were updated repo-wide and the
  new packages join the import-linter "no `server`/`operator_api`" layering contract
  (#866). Forks merging this re-point their imports of these modules (e.g.
  `import metrics` → `from observability import metrics`; `import paths` →
  `from infra import paths`).

### Removed
- **The Gradio chat UI (the `--ui full` tier).** `chat_ui.py`, the `gradio` / `ui`
  optional dependency, and `requirements-ui.txt` are gone — the React console is the
  only UI. Deployment tiers are now **`console` (the new default)** and **`none`**
  (ADR 0010, amended). `--ui full` / `PROTOAGENT_UI=full` is kept as a **deprecated
  alias for `console`** (logs a warning) so existing invocations don't break, and a
  bare `/` now redirects to the console at `/app`. The Docker image drops the
  conditional `UI=full` install (it pulled the removed extra) and always installs the
  lean core; the console ships as static assets, not a pip dep. **Migration note:** the
  non-streaming `chat()` thread_id prefix is renamed `gradio:` → `chat:`, so any
  in-flight non-streaming (OpenAI-compat) conversation re-keys once on upgrade
  (streaming/A2A sessions, keyed `a2a:`, are unaffected).

### Fixed
- **The desktop app reported its version as `0.0.0` (version-coherence Cross-cutting
  B).** A frozen PyInstaller binary has no installed-package metadata, and
  `pyproject.toml` wasn't bundled — so `paths.package_version()` fell through to its
  `0.0.0` last resort, which blinds the A2A card, the fleet version handshake, runtime
  status, and the plugin `min_protoagent_version` compat gate (every plugin that sets
  one was wrongly refused on desktop). `pyproject.toml` is now bundled into the
  sidecar (`build_sidecar.py`), so the existing `_MEIPASS` read resolves the real
  version. (Docker already worked via `COPY .`.)
- **Fleet members render plugin views with no design system (version-coherence
  Axis 3).** The DS plugin-kit (`/_ds/plugin-kit.{css,js}`) was served only by the
  console tier (`mount_react_app`), so a `--ui none` fleet member served its plugins'
  view *pages* but 404'd the kit they `<link>` — proxied plugin views rendered
  unstyled. The kit now mounts in **every** tier via a dedicated `mount_ds_plugin_kit`,
  independent of the console SPA.

### Added
- **The plugin devkit can now build a plugin AND run it live — no restart** (ADR
  0027/0040). `scaffold_plugin` used to write a skeleton and tell you to "add it to
  `plugins.enabled` and restart"; it now **enables + hot-reloads** what it scaffolded
  (the same path the console enable toggle uses, #822), so the new plugin's tools/view
  are live on the agent's next turn. The edit→test loop is closed with two new devkit
  tools: `reload_plugins` (re-execs enabled plugins so an edit to a plugin's
  `__init__.py` goes live) and `enable_plugin(id)` (turn on any on-disk plugin live).
  Communication plugins (ADR 0029) still enable from Settings (they need a token).
- **`plugin new` / `plugin new-bundle` CLI** — scaffold a plugin or an ADR-0040
  bundle from the shell: `python -m server plugin new "My Plugin" --view --skill`,
  `… plugin new-bundle "My Stack" --member board=url@ref --builtin delegates`. The
  writers moved to core (`graph.plugins.scaffold`) so the CLI works without the devkit
  plugin enabled; the devkit tool is now a thin wrapper that adds the live-enable.
- **Spin local fleet members down when the host exits (version-coherence Axis 1).**
  Members are spawned detached (so they survive the launching CLI) — but that also
  let a member outlive a hub rebuild+restart and keep running *old* code. The hub now
  stops its local members on shutdown by default ("host down → fleet down"); sessions
  resume from their `instance.id`-scoped checkpoints on the next switch, so it stops
  processes, not work. Opt out with `PROTOAGENT_FLEET_KEEP_MEMBERS_ON_EXIT=1` for
  long-running detached agents. Hub-only (a member's scoped registry is empty),
  bounded teardown (concurrent SIGTERM → one shared wait → SIGKILL stragglers). See
  `docs/dev/version-coherence.md`.

## [0.34.0] - 2026-06-10

### Fixed
- **CSS comment corruption that silently shrank plugin iframes (build guard).**
  A `*/` written inside a CSS comment — e.g. a class glob like
  `.plugin-install-*/.plugin-list` in prose — closes the comment early, so
  esbuild parses the rest as CSS, emits a recoverable `css-syntax-error` *warning*,
  and drops tokens. A real rule downstream can vanish from the bundle while the
  build still "succeeds" (this is the root cause behind the tiny-plugin-iframe
  reports: a dropped `.plugin-view` rule fell back to the stage-panel grid). Fixed
  the two latent instances in `chat.css` and `theme.css`, and added a
  `prebuild` guard (`scripts/check-css-comments.mjs`) that **fails the build** on
  any `*/` glued to identifier characters inside a `src` CSS file — so this class
  of corruption can never reach `dist` silently again.
### Added
- **Design-system 0.26 + slug-aware plugin-kit `apiFetch`/`apiUrl` (protoContent#208).**
  Bumped `@protolabsai/ui` to 0.26, whose served plugin-kit now derives the
  `/agents/<slug>/` fleet-proxy base itself — a plugin view's data call is just
  `kit.apiFetch("/api/plugins/<id>/x")`, no manual
  `location.pathname.split("/plugins/")[0]` prefixing, and it stays correct on the
  host window **and** through the fleet proxy (ADR 0042). View-authoring rule 3 is
  now automatic for data. Updated the `chat_example` gold-standard + the
  building-a-view guide (rule 3 + the kit-helper table, now documenting the new
  `apiUrl`) to model the simpler pattern; the only thing a view still base-prefixes
  by hand is the kit's own `<link>`/`<script>` (they load before the kit exists).
  0.26 is a **kit-only** DS release — no console component changed (verified by
  diffing the package), so the bump carries no console visual risk.
- **Plugin update / version-awareness (ADR 0027 follow-on).** Git-installed
  plugins now show whether they're current and can be updated in place. A new
  `GET /api/plugins/updates` reports per-plugin freshness — `git ls-remote` the
  recorded `source_url` at its ref vs the locked `resolved_sha` (timeout-bounded
  + TTL-cached so the UI poll can't hang or hammer the remote); a SHA-*pinned*
  plugin skips the network entirely (it never auto-updates), and any lookup
  failure is reported per-row without breaking the rest. `POST /api/plugins/{id}/update`
  pulls the latest code at the recorded ref (force re-install → rewrites the lock)
  and, if the plugin is enabled, hot-reloads through the same path the enable
  toggle uses (#822) so the new code mounts without a restart — first dropping the
  plugin's whole `sys.modules` subtree so a multi-file plugin re-imports fresh code
  rather than serving a cached submodule. The Plugins rail (Local tab) and Settings →
  Integrations both render a DS `Badge` freshness indicator next to the version
  (up to date · update available · pinned · check failed) and an **Update** button
  when behind, with the same restart-hint contract the enable flow uses.

## [0.33.0] - 2026-06-10

### Added
- **Architectural import contracts in CI** — `lint-imports` (import-linter,
  pinned) now gates three layering contracts declared in `pyproject.toml
  [tool.importlinter]`: `graph/` and the infra packages
  (`events`/`knowledge`/`runtime`/`scheduler`/`tools`) must not import
  `server/` or `operator_api/`, and `operator_api/` must not import `server/`.
  The 8 existing violations (e.g. `graph.skills.cli -> server.agent_init`, the
  `operator_api` route modules reaching into `server.agent_init`/`server.chat`)
  are grandfathered as an explicit burndown list in `ignore_imports` — new
  violations fail CI, including function-level (lazy) imports. (#866)
- **Hub↔remote version handshake — fleet version skew is visible now** (audit N5).
  The console↔server `/api/*` surface has no versioning, and a remote fleet member
  (ADR 0042 §I) makes skew real: the hub console drives a *different release* by
  proxy. The remote-reachability probe now also lifts the remote's app version off
  its A2A agent card (same unauthenticated request, no extra round-trip) and
  persists it on the registry record; `/api/fleet` carries `version` on every
  member (the hub's own on the `host` entry, never any token), and
  `/api/runtime/status` reports the serving instance's `version`. Settings →
  Agents shows a warning badge on a remote whose version differs from the hub's
  ("remote runs vX.Y.Z, hub vA.B.C — features may misbehave"). Also:
  `remotes.json` mutations now serialize on their own sibling FileLock
  (`remotes.json.lock`) instead of sharing `fleet.json`'s, so remote add/remove
  and probe-version persists can't contend with — or be lost under — fleet-state
  writes. (#868)
- **Design-system 0.25 adoption + `theme.css` decomposition (#832).** Bumped
  `@protolabsai/ui` to 0.25 and replaced the console's hand-rolled chrome with DS
  components — `Splash`/`BootGate` (boot/splash), `EditableText` (inline rename),
  `Empty`/`Grid`/`Badge`, the `ToolCard` family (chat tool calls), and `TabBar`
  (chat session tabs, using 0.25's responsive collapse). The 3,387-line monolithic
  `apps/web/src/app/theme.css` was carved into co-located per-surface CSS modules
  (Axis-A) and shrunk as each surface adopted the DS (Axis-B) down to ~1,900 lines
  of genuinely-shared shell/base. (#854, #859, #860, #861, #862, #863, #864, #881)
- **Layered settings cascade (ADR 0047) + settings IA (ADR 0048).** Per-field
  App→Host→Agent override via `Field.scope` (git-style nearest-wins, `host-config.yaml`
  holds box-shared defaults), surfaced as two scope-based settings homes — Host/App
  (box-shared; the host is the first agent) and Workspace (the focused agent). (#844, #880)
- **Plugin-view authoring hardening (#884).** The DS plugin-kit JS is now served
  same-origin at `/_ds/plugin-kit.js` (`initPluginView`/`apiFetch`/`getToken`), so
  views stop re-rolling the theme + hardcoding URLs. The loader warns when a declared
  `views[].path` is served by no router, or when a plugin registers a second router
  at a colliding `(plugin_id, prefix)` (silently dropped at mount); the manifest
  warns on non-same-origin view paths. The contradictory pair of guides collapses
  into one canonical guide with the four view rules (serve-what-you-declare · gate
  the data not the page · same-origin slug-aware · link the kit), the postMessage
  handshake + event-bus + sandbox contract, and the `chat_example` gold-standard.

### Fixed
- **Light mode works on the hand-rolled chrome (#842).** The `:root` token bridge
  defined `--bg`/`--fg`/`--error` but not the `--bg-elevated`/`--fg-tertiary`/
  `--danger` synonyms the chrome used (8–14× each), so they fell back to a dark
  literal and never flipped; aliased them to the matching `--pl-*` tokens, and
  tokenized the remaining ~40 raw hardcoded colors across the carved modules so
  they flip with the theme. (#854, #862)
- **Enabling a plugin's console view works immediately — no restart, no blank
  panel (#853).** A console view is just an iframe over a hot-mounted router route
  (#822), so the "restart required" prompt on enable was stale (restart now flags
  only on *disable*, which can't unmount a route). `PluginView` status-probes the
  route before mounting the iframe — a same-origin 404 fires `onLoad`, not
  `onError`, so the old code rendered the bare 404 body as the "view" — and surfaces
  an actionable error instead of a blank panel.
- **Plugin views resolve on fleet members, not the hub (#879).** `apiUrl()` routed
  `/api/*` to the focused agent but not the default `/plugins/<id>/…` view prefix,
  so a member's view iframe hit the hub (which lacks that plugin) → 404 / "refused
  to connect". `isAgentPath()` now matches `/plugins/` too.
- **Host defaults renders as one cohesive panel (#878)** — it rendered one full
  panel per category (Agent + System), stacking duplicate Save bars + explainers;
  now a single panel aggregating the host-scoped fields across categories.
- **A single Ctrl-C shuts the server down cleanly (#882).** `uvicorn.run` had no
  `timeout_graceful_shutdown`, so it waited indefinitely on long-lived SSE /
  fleet-proxy connections and forced a second Ctrl-C whose `KeyboardInterrupt`
  dumped `CancelledError` tracebacks; bounded to 5s.
- **`config_to_dict` now emits the complete plugins section** — the serialized
  config dict (the `/api/config` payload and anything else treating it as the
  full config) carried only `plugins.{enabled, dir}`, silently dropping
  `plugins.disabled` and `plugins.sources.allow` (2026-06-10 prod-readiness
  audit, N6). The YAML file itself was never at risk — saves merge in place and
  never delete absent keys — but dict consumers lost the values and the
  Settings UI could never surface them; this unblocks the plugin-hardening
  work that writes `sources.*`. A new drift-guard test also pins the third
  triplet direction: `LangGraphConfig.from_dict` must consume every settings
  FIELDS key with a non-default sentinel (a missing parse line used to mean
  the YAML held the value, the UI showed it saved, and the runtime silently
  read the default — audited: zero such drops today). (#865)
- **A2A task records no longer accumulate forever on an always-on agent** — the
  24h task-TTL sweep ran only inside `initialize_a2a_stores` at boot, so a
  long-running process grew `a2a-tasks.db` unbounded between restarts. The
  sweep now also runs from the existing hourly prune loop (alongside the
  checkpoint + telemetry pruning), best-effort with a log line.
- **Webhook DNS resolution no longer blocks the event loop** — the push-callback
  SSRF guard (`is_safe_webhook_url`) calls `socket.getaddrinfo` synchronously,
  and it ran *on* the loop at push-config set-time and before **every** push
  POST (the send-time re-validation backstop) — one slow resolver stalled every
  stream, health check and A2A peer for the OS timeout. Both async call sites
  now dispatch the check via `asyncio.to_thread`; the guard itself stays sync
  and its policy is unchanged.
- **`min_protoagent_version` is actually enforced** — the plugin manifest field
  was parsed and documented as a compat guard ("warn/refuse on an older host")
  but never compared against anything. The loader now refuses to load an
  enabled plugin that declares a newer minimum than the running host (clear
  `log.error` naming both versions, surfaced in the plugin's status meta,
  before any plugin code imports); a malformed version string on either side
  only warns and loads, so a typo can't brick a plugin. Adds `packaging` to
  `[project.dependencies]` (it was only a transitive dep; the loader now
  imports it directly).
- **Autostart launches the server again** — the macOS LaunchAgent installer still
  pointed at the single-file `server.py` that ADR 0023 promoted into the `server/`
  package: the install-time existence check always failed (the login-launch toggle
  was dead), and any plist installed before the rename crash-looped at login. The
  plist now runs `python -m server` with the repo root as `WorkingDirectory` +
  `PYTHONPATH` (the `entrypoint.sh` recipe); re-enabling autostart overwrites a
  stale plist in place. The CI stale-path guard — which only scanned
  `*.sh`/`*.yml`/`Dockerfile*` and so missed this — now also covers `*.py`. (#855)
- **Knowledge embedding no longer blocks the event loop** — with a hybrid store,
  the query embed (a sync HTTP call) ran *on* the loop before **every** LLM call
  (`abefore_model` just called the sync hook), and inside the async
  `memory_recall`/`memory_ingest` tools and `/api/knowledge/search` — one slow
  embedding endpoint stalled every stream, health check and A2A peer on the
  server. All four paths now dispatch via `asyncio.to_thread`, same as the
  checkpointer. (#857)
- **Chat no longer rewrites localStorage on every streamed token** — the console
  chat store serialized *all* sessions to localStorage per SSE frame (~24 chars),
  each write firing a cross-window `storage` event the other fleet windows
  re-parse. Streamed updates now persist on a trailing 300ms timer; session
  add/remove/rename/switch, stream done and page unload still flush immediately,
  and the UI still streams live (only the write is deferred). (#857)

### Security
- **Token-less non-loopback binds now refuse to start.** Binding a host other
  than loopback with no A2A auth token used to log a warning and boot anyway —
  leaving the full operator API (plugin install+enable = code execution,
  config/SOUL rewrite, subagent runs) open to anything that could reach the
  port. The boot gate (`a2a_auth.evaluate_open_bind`) now exits with an error
  unless `PROTOAGENT_ALLOW_OPEN=1` explicitly opts in for fenced deployments.
  The bundled `docker-compose.yml` publishes the port to **127.0.0.1 only** by
  default, passes `A2A_AUTH_TOKEN` through, and opts in (the localhost publish
  is its boundary). **Upgrade note:** an existing deployment binding
  `0.0.0.0` without a token must set `A2A_AUTH_TOKEN` (recommended) or
  `PROTOAGENT_ALLOW_OPEN=1` to boot.
- **Persistence hardening — atomic writes, a config write lock, and 0600 on the
  remote-token registry** (prod-readiness audit). `langgraph-config.yaml`,
  `fleet.json`, `remotes.json`, and `workspace.yaml` were written with a bare
  `open(path, "w")` — a crash mid-dump left a truncated file, and the fleet
  registries silently loaded `{}` afterwards (every running agent forgotten,
  every remote member + stored bearer dropped, zero log lines). All four now
  land via a shared `paths.atomic_write` (same-dir temp + `os.replace`);
  corrupt registries still load tolerantly but WARN loudly. `remotes.json`
  is now written 0600 (it carries remote bearer tokens — the "same posture as
  secrets.yaml" its comment claimed but didn't have). Concurrent settings
  saves (two console windows, a save racing a plugin toggle) were a classic
  lost-update on the YAML plus interleaved graph reloads — `_apply_settings_changes`,
  `_reset_settings_keys`, and `_reload_langgraph_agent` now serialize on one
  RLock.
- **Pinned the release-tools clone in the PR gate** — `checks.yml` cloned
  `protoLabsAI/release-tools` at HEAD and executed its script on every PR, so
  a push to that repo's `main` could change what runs in this repo's CI. The
  clone is now pinned to a commit SHA (v2.3.0), matching the action pin
  `release.yml` already uses. (#866)

## [0.32.0] - 2026-06-10

### Added
- **Layered settings cascade — host-shared defaults agents inherit and override**
  (ADR 0047). Settings now resolve **App → Host → Agent** per field. A new **Host
  defaults** tab sets box-shared defaults — model/gateway, routing, prompt-cache,
  telemetry, org branding — that every agent on the machine inherits; each agent
  overrides any of them in its own settings (git-style: nearest layer wins), with
  **"inherited from Host" / "overridden here"** badges and one-click **Reset to
  inherited**. The shared layer lives in `host-config.yaml` (per-hub, `scope_leaf`'d);
  secrets stay agent-local (never written to the host file). No migration: with no
  host file the cascade is byte-identical to the old single-config behavior.
  (#833/#836/#838/#846/#847/#848/#849)
- **Remote fleet members — the agent there, the UI here** (ADR 0042 §I). Register any
  reachable protoAgent by URL (Discover → *Add to this fleet*, or
  `POST /api/fleet/remotes`) and it becomes a switchable member: a slug window like a
  local peer, console + A2A reverse-proxied through the hub, with the remote's bearer
  attached server-side. Run agents fully headless on other machines and operate them
  all from one console. (#839)
- **Tenant guard** — when a *different* backend reuses this console's address (a port
  handed between agents), the previous tenant's persisted chat view is dropped (one
  reload + a toast) instead of rendering another agent's transcripts. Same-agent
  restarts/upgrades never trip it. (#831)
- **Tailnet discovery** — fleet discovery gains a third channel: online **Tailscale**
  peers (via the local `tailscale` CLI) are probed for agent-cards over the fleet port
  range, since mDNS multicast never crosses a WireGuard overlay. All three channels
  (local scan, mDNS, tailnet) now scan concurrently. (#816)
- **Co-located-instance warning** — every server drops a heartbeat in its data root;
  when a LIVE sibling shares the same root (two unscoped instances, or two with the
  same `PROTOAGENT_INSTANCE`), both consoles banner it and the boot log warns — they
  can clobber each other's chat history, knowledge and stores. (#818)
- **Cross-agent "turn finished" toasts** — leave a turn running on one agent, switch
  windows, and get a toast (+ a native notification when the window is hidden) the
  moment it completes. The shell watches the other agents' in-flight turns and polls
  their durable tasks through the hub proxy. (#827)
- **Opaque agent ids + rename** — fleet agents get a stable, opaque id at create
  (`ava-4e8e`) that keys the workspace, the window URL and the data scope; the *name*
  is now an editable display label (pencil-rename in the fleet manager,
  `PATCH /api/fleet/{agent}`). Renames never move storage or break open windows. (#823)
- **Enable delegates without a restart** — plugin routes now hot-mount on a config
  reload, so enabling a route-bearing plugin (e.g. `delegates` on the host) takes
  effect immediately; the fleet manager turns the old "needs a restart" dead-end into
  a one-click **Enable delegates on this agent** that retries the add. (#822)
- **Cold agents resume on navigation** — opening a stopped agent's window now
  activates it (resume from checkpoint + keep-N-warm touch) instead of hitting a dead
  proxy. (#819)

### Fixed
- **Discover no longer lists a co-located agent twice** — its mDNS advert (LAN IP) now
  collapses with the local-scan hit (loopback), and a fleet peer's own advert no longer
  reappears as "discovered". (#837)
- **mDNS advertise actually works** — `Zeroconf.register_service` was called on the
  event loop and deadlocked it: a ~10s stall at every boot, then a swallowed failure,
  so **no agent had ever advertised** since the feature shipped. Now runs off-loop,
  with a guard that refuses (loudly) instead of stalling. (#815)
- **A2A task reconcile had rotted against a2a-sdk 1.1** — the chat self-heal and
  cancel used the 0.3 method names (`tasks/get`/`tasks/cancel` → Method not found),
  which made an interrupted turn finalize instantly even while still running on the
  server. Fixed to the 1.0 wire (`GetTask`/`CancelTask` + `A2A-Version` header); the
  e2e mock now mirrors the real wire and rejects the legacy names so this class of
  rot can't pass CI again. (#827)
- **Each fleet hub owns its own registry** — `~/.protoagent/workspaces` (and
  `fleet.json`) is now instance-scoped like every other store, so two co-located
  instances no longer manage/evict each other's agents, and a peer can no longer see
  or stop its parent hub's fleet. (#813)

### Changed
- **`pyproject.toml` is the dependency source of truth** — runtime deps moved into
  `[project.dependencies]` / `[project.optional-dependencies]`, so `uv sync` and
  `pip install -e .[ui,google]` both just work; `requirements-*.txt` are kept as
  readable, tier-scoped references that mirror it. (#811)
- **Config is a single source of truth** — `config_to_dict` is now driven by the
  settings-schema `FIELDS` registry (it had silently drifted, dropping 27 fields),
  with a `from_dict` parse seam and a drift guard; adding a setting is now one
  `Field` declaration that flows to parse, serialize, and the UI. (#833/#836/#838)
- **Shell + settings banners are the design system's `Alert`** — both hand-rolled
  banner implementations replaced by `@protolabsai/ui` `Alert`; the genuinely missing
  inline-rename control is filed upstream instead (protoContent#195), per the
  contribute-back loop now recorded in `docs/design/ui-component-audit.md`. (#825, #827)

### Removed
- **Retired the deprecated `peer_consult` / `peer_list` tools** from the core
  toolset. `delegate_to` over the unified delegate registry (ADR 0025,
  `plugins/delegates`) has been the federation path since v0.16.0 — it does A2A
  consult alongside openai/acp delegates behind one tool with a console panel.
  The env-var `PEER_<HANDLE>_URL` tools are gone; the a2a adapter retains the
  shared A2A response parse helpers (`tools/peer_tools.py`).

## [0.31.0] - 2026-06-10

### Changed
- **Intro splash shows once per session** — the launch bumper is gated by `sessionStorage`, so a
  refresh no longer replays the 2.5s splash; a fresh tab session sees it once. (Automation still skips it.)
- **Plugin devkit refreshed (v0.2.0)** — the reference plugin + scaffolder now models current best
  practice: console views are sandboxed iframes served under `/api/plugins/<id>` (bearer-gated, ADR
  0038/0026), and the event bus (ADR 0039) is first-class — the scaffold stubs + the `building-plugins`
  skill + the `plugin-architect` show `registry.emit`/`on` and manifest `emits:`/`subscribes:`, and the
  devkit itself emits `plugin-devkit.scaffolded`.
- **Artifact plugin is now external** — extracted from core to
  [protoLabsAI/artifact-plugin](https://github.com/protoLabsAI/artifact-plugin) (git-installable,
  `protoagent-plugin` topic). It's the reference distributable plugin; core ships leaner. Install via
  Plugins → Download.
- **Design system → @protolabsai/ui 0.18, with console polish** — the Identity panel renders SOUL.md
  as Markdown by default (an **Edit** toggle flips to a raw editor) and fills the panel; a
  **left-panel collapse toggle** joins the right one (both drag-aware; click an open panel's rail
  icon to close it); chat-composer height + delegate-badge layout fixes.

### Removed
- **The `/active` global-pointer proxy machinery** — superseded by slug routing (`/agents/<slug>/*`);
  the `activate` endpoint is now ensure-running + keep-N-warm.
- **Retired Module Federation (ADR 0038)** — plugin UI is now **sandboxed iframes** only
  (the right model for untrusted third-party + generative code, and trivially git-installable).
  Removed the in-process `ui: react`/federation path, the `@protoagent/plugin-ui` federation SDK,
  the react-vs-iframe **trust gate** (`plugins.trusted`, the allowlist, the "Trust React" toggle),
  `FederatedView`, and the host remotes. **Notes** is now a self-contained iframe plugin (serves
  its own editor page). The context-menu registry moved back host-internal. Guide rewritten.

### Added
- **Fleet console — run a fleet of agents from one console (ADR 0042).** A slug-routed UI
  (`/app/agent/<slug>/`) where each window targets its own agent, so two agents can be open in two
  windows at once with no shared-state cross-talk. Includes a **fleet manager** (create / start /
  stop / remove agents) + an **archetype picker** (Basic + a built-in **Project Manager** that clones
  the latest pm-stack on create), a **topbar switcher**, and **per-agent layout / theme / chat**.
  New agents inherit the host's model config (model-only) so they boot ready-to-chat on the same
  gateway. Agents are addable as each other's **`delegate_to` targets** for agent-to-agent flows,
  and **mDNS + local-scan discovery** finds other protoAgents on the box / LAN to add as remote
  delegates.
- **Chat panel is a slot (ADR 0045)** — a plugin can contribute a `slot:"chat"` view that replaces
  the built-in chat panel (A2A stays the canonical contract).
- **Plugin-driven console navigation (ADR 0044)** — plugins drive surface navigation via
  `registry.navigate`.
- **Goals come alive in the console** — the Goals panel now shows a **monitor** badge + last-checked
  (vs drive iteration count), and a goal finishing raises a **toast** (`goal.achieved`/`goal.failed`,
  ADR 0039). Authoring stays in chat (`/goal`); the panel is observe + clear. Goal-mode guide updated.
- **Goals broadcast on the event bus** — a terminal goal now emits `goal.achieved` / `goal.failed`
  (ADR 0039) with `{session_id, condition, status, reason, evidence, mode}`, alongside the existing
  plugin `goal_hooks`. **Any plugin (or the console) can react to a goal completing without writing a
  goal-hook plugin** — the decoupled flywheel (no cross-plugin dependency).
- **Telemetry opt-out in Settings** — `telemetry.enabled` (+ retention) are now a console toggle
  (System → Telemetry), not YAML-only. Off = no store is opened and the per-turn record path no-ops;
  telemetry is local and never sent anywhere. (Memory/knowledge middleware were already toggles.)
- **Plugin notification dots + event relay (ADR 0039 S2)** — the console subscribes to the bus;
  a `<plugin>.*` event lights that plugin's rail icon until its surface is opened (no badge endpoint,
  no polling). The client SSE dispatcher routes by topic with `*`/`#` wildcards; the plugin-view
  bridge is now bidirectional — sandboxed pages `protoagent:subscribe` to topics, receive
  `protoagent:event`, and `protoagent:publish` (host-stamped to the plugin's namespace).
- **Plugin event bus (ADR 0039)** — promotes the ADR 0003 bus into a decoupled topic pub/sub:
  dot-namespaced topics with `*`/`#` wildcards, in-process handler subscriptions (`registry.on`),
  namespace-guarded publish (`registry.emit` auto-prefixes `<plugin>.`), a ring buffer for SSE
  reconnect catch-up (`GET /api/events?since=`, frames carry `id:`/seq), and a gated
  `POST /api/events/publish` for client/iframe publishes. Plugins declare their contract via
  `emits:`/`subscribes:` in the manifest. The no-cross-plugin-dependency clause: the bus is the only
  inter-plugin channel; nobody imports anyone.
- **Fork extension seam (ADR 0038 slice 3)** — a build-time **`src/ext/`** seam: a fork drops a
  `*.tsx` that calls `registerSurface()` / `registerContextMenu()`; the console auto-loads it via
  `import.meta.glob`. **Core ships the directory empty**, so `git pull upstream` never conflicts on
  a fork's additions. The trusted, in-process, fork-owned path — distinct from sandboxed plugins.
  Completes the two-mode plugin-UI model (ADR 0038).
- **Generative-UI artifacts (ADR 0038)** — a first-party `artifact` plugin: the agent calls
  `show_artifact(kind, code)` to render HTML / SVG / Mermaid / React on demand into a sandboxed
  iframe (the Claude Artifacts / Open WebUI model). Plus a `rendering-artifacts` skill so the
  agent reaches for it over writing files.
- **Generative-UI artifacts (ADR 0038)** — a first-party **`artifact`** plugin: the agent calls
  `show_artifact(kind, code)` to render **HTML / SVG / Mermaid / React on demand** into a
  **sandboxed iframe** (`sandbox="allow-scripts"`, no same-origin) — the Claude Artifacts / Open
  WebUI model, so generated code is isolated from the console. Rides the existing iframe surface
  path (no federation). First slice of the two-mode plugin-UI model (ADR 0038); the `src/ext` fork
  seam + Module Federation retirement follow.

### Security
- **Secret-scan CI gate** — gitleaks runs on every PR (plus an opt-in pre-push hook), blocking
  secrets from reaching the repo; example/lockfile/doc paths and the redaction-test fixtures are
  allowlisted to avoid false positives.

## [0.30.0] - 2026-06-09

### Added
- **Notes plugin — the first-class React reference plugin (ADR 0034 slice 4)** — a greenfield
  `notes` plugin replaces the legacy native Notes: one shared markdown doc (no tabs/undo/
  versioning), instance-scoped, owned by the plugin. It registers the agent tools
  `read_note`/`write_note`/`append_note`, a bearer-gated data route, and a `ui: react` console
  panel (single-panel editor + preview toggle + autosave) mounted in-process (it's on the shipped
  trust allowlist). **Replaces the legacy native Notes** — the old workspace/tabs/undo surface, the
  `notes_*` tools, and the `operator_api/notes` store + `/api/notes` routes are all removed. New
  guide: *Building a React plugin view*.
- **Plugin trust gate (ADR 0034 slice 3)** — a `ui: react` plugin mounts **in-process only if
  host-trusted** (a shipped first-party allowlist ∪ the operator's `plugins.trusted`); an untrusted
  `ui: react` view **degrades to a sandboxed iframe**. Trust is **host-decided, never plugin-
  declared** — deny-by-default. New `POST /api/plugins/{id}/trusted` + a **"Trust React"** toggle
  in the Plugins surface so the operator can promote a plugin.
- **Plugin-UI SDK: host bridge + reference remote (ADR 0034 slice 2)** — `@protoagent/plugin-ui`
  now exposes a **host bridge** (`setHostBridge`/`getHostBridge`: the authed API client, `authToken`,
  `apiUrl`, `brandName`) so a remote gets host context without importing host internals. The
  `hello-react` reference remote **consumes the SDK**: it registers a context-menu item that
  appears in the host's rail menus — the end-to-end proof that a federated plugin extends the
  console's menus across the boundary (ADR 0036).
- **Plugin-UI SDK foundation (ADR 0034 slice 2)** — a new versioned **`@protoagent/plugin-ui`**
  package now holds the context-menu registry/store/types, and the host shares it as a **Module
  Federation singleton** — so a `ui: react` remote gets the *same* registry instance and a plugin
  can **`registerContextMenu`** into the host's menus (ADR 0036's extension point, cross-boundary).
  The host re-exports it (no behaviour change). The host bridge (API/auth, QueryClient, theme,
  shell pieces) + the reference remote consuming it land next. (No `@protolabsai/ui` dependency —
  unblocked from its publish.)
- **Mobile shell (ADR 0035 slice 4)** — below 768px the console drops the dual-rail split for a
  single-surface view with a **bottom quick-bar** (configurable, default Chat/Activity/Knowledge/
  Plugins) + a **hamburger drawer** listing every surface. Chat stays mounted (streaming
  continuity). Breakpoint-driven off the same store; desktop unchanged. (Drawer is interim —
  swaps for `@protolabsai/ui`'s Drawer when it lands.)
- **Everything-swappable rails (ADR 0036)** — plugin views are now first-class `railOrder`
  members (reconciled in/out as plugins come and go), and **Chat is movable too** (it mounts on
  whichever rail holds it, preserving streaming continuity). Right-click any surface → **Move up /
  Move down / Move to other rail**. The rail is now an extraction-ready `<SurfaceRail>` component.
- **Right-click context menus (ADR 0036 slice 1)** — an app-wide context-menu system on shadcn
  Radix `DropdownMenu`: a registry keyed by `ContextType` (core *and* plugins register items,
  merged by priority + deduped), an imperative `openContextMenu(type, e, ctx)`, and one
  `<ContextMenuRenderer>`. First menu: **right-click a rail icon → Move to other rail** (the
  surface-swap trigger, replacing the removed hover buttons). `registerContextMenu` is the plugin
  extension point (to be exposed via the plugin-ui SDK).
- **Design-system foundation (ADR 0037 slice 1)** — the console adopts **Tailwind + the
  `@protolabsai/design` preset/tokens + shadcn/Radix**. Tailwind runs with preflight off so it
  coexists with the legacy `theme.css` (incremental migration); a shadcn→token bridge maps the
  component theme onto the `--pl-*` brand tokens (one dark-first theme); ships the `cn` util + a
  pilot `Button` (first owned-source component, swapped into Settings). The base the context menu
  + future components build on.
- **Swap surfaces between rails (ADR 0035 slice 3)** — one `renderSurface(id)` now mounts any
  surface in either rail, and a hover affordance on a rail icon moves it to the other side
  (persisted). A surface lives on exactly one side. Chat stays pinned left (it mounts
  unconditionally for streaming continuity).
- **Resizable right panel — real handle (ADR 0035 slice 3)** — the divider is now a proper
  grab target (14px hit area, visible grip that thickens on hover/focus) and **keyboard-resizable**
  (←/→ nudge, Shift = bigger step, Home/End = max/min) with **double-click to reset**. Width still
  persists via the UI store.
- **Symmetric dual rails (ADR 0035 slice 2)** — the right panel's horizontal segmented tab
  strip becomes a vertical **right rail** mirroring the left (same `RailButton` component) on the
  far edge: [left rail | left surface | right surface | right rail]. Picking a right surface
  (Notes/Beads/Goals/Schedule + plugin right-views) expands it. First step toward swappable
  surfaces (slice 3) + mobile (slice 4).
- **Persisted UI state (ADR 0035 slice 1)** — the console's navigation/layout state (active
  surface, sub-tabs, right-panel width/collapse) now lives in a Zustand `persist` store, so a
  **refresh restores where you were** instead of snapping back to Chat/Notes. Pure state migration
  — no visible layout change yet; the foundation the dual-rail/mobile slices build on.
- **Plugin UI — first-class React (ADR 0034, slice 1)** — the console is now a Module
  Federation *host*: a plugin view declaring `ui: react` mounts a federated React **remote**
  into the console's own tree (sharing the host's React 19 + react-query — one instance, one
  cache), instead of an iframe. Ships the `FederatedView` runtime loader with a fail-safe error
  card (a bad remote never white-screens the console), the `ui`/`remote` manifest fields, and a
  `hello-react` reference remote (right panel). `ui: iframe` stays the default for untrusted
  third-party plugins.

### Fixed
- **ACP persona reaches GitHub Copilot** — Copilot CLI didn't adopt the configured persona
  (it answered as "GitHub Copilot CLI") because it reads `.github/copilot-instructions.md`, not
  just `AGENTS.md`. The ACP runtime now also writes the agent's canonical file (Copilot's under
  `.github/`); verified live — Copilot answers as your agent.
- **ACP turns attributed correctly in telemetry** — they were recorded under the gateway
  model (`protolabs/reasoning`, which never ran) with no model of their own. The ACP path now
  emits a usage frame tagging the turn `acp:<agent>`; gateway tokens/cost stay 0 because the
  external agent's own subscription meters usage (the `acp:` label is the signal it wasn't
  gateway-metered).

### Changed
- **Console upgraded to React 19** — `apps/web` moved React 18.3 → 19.2 (already on `createRoot`
  with no removed-API usage, so a clean bump; all 60 e2e pass). Sets the shared singleton for the
  ADR 0034 plugin-UI federation harness.

## [0.29.0] - 2026-06-08

### Added
- **ACP answer-text streams** — the coding agent's reply now streams to the chat as it's
  produced (answer-text deltas forwarded as `text` frames, interleaved with tool cards in
  order), instead of landing all at once when the turn completes. Granularity follows the
  agent (proto sends coarse chunks; token-streaming agents render finer).

## [0.28.0] - 2026-06-08

### Added
- **ACP tool calls surface as cards** — the coding agent's tool calls (its own + the operator
  MCP tools) now stream as `tool_start`/`tool_end` to the chat, same as the native runtime,
  instead of only the final answer.
- **ACP runtime adopts your persona** (ADR 0033) — `SOUL.md` is written as `AGENTS.md` (+ a
  vendor file) into the coding agent's session workspace, so it loads your agent's identity into
  its own system prompt and answers as your agent, not generic "Codex/Claude". The session runs
  in a dedicated instance-scoped workspace (not your repo); the persona is injection-scanned.
- **Runtime selector leads the Agent settings** — the Agent runtime group is now first in
  Agent → Settings, with an active-runtime badge in the header and a banner (when an ACP
  runtime is active) explaining the model settings still power protoAgent's own aux calls.
- **Auto-scoping for co-located instances** (#706) — set `PROTOAGENT_AUTO_SCOPE=1` and an
  instance with no explicit `PROTOAGENT_INSTANCE` derives a stable per-working-directory id, so
  instances on one machine never silently share `~/.protoagent` and clobber each other's goals/
  knowledge/checkpoints. Opt-in (relocating existing unscoped data is deliberate); regardless,
  the server now **warns loudly at boot** when running unscoped against a non-empty data home.
- **ACP-only setups need no gateway** (ADR 0033) — when the runtime is `acp:<agent>` and no
  OpenAI-compatible gateway key is set, protoAgent's auxiliary LLM calls (compaction, goal
  verification, fact extraction) fall back to the same coding agent via an `AcpChatModel`
  adapter, and headless validation no longer requires a gateway. (Embeddings still need an
  embed endpoint, else semantic recall degrades to keyword — unchanged.)
- **Agent runtime selectable in the console** — Agent → Settings has an **Agent runtime** group:
  a dropdown (native | acp:proto | acp:codex | acp:claude | acp:copilot | acp:opencode) + a
  **tools allowlist** for the ACP brain. The allowlist accepts `*` to expose everything (minus
  `execute_code`, which a coding agent already has) — no need to enumerate every tool.
- **ACP delegate teardown** — `coding_agent.evict_client(spec)` + `AcpAdapter.teardown(delegate)`
  evict the cached `AcpClient` for a spec **and** terminate its subprocess (a plain cache `pop`
  forgot the handle but left the child running). Completes the delegate lifecycle for callers that
  dispatch into a transient, per-call `workdir` (e.g. a disposable git worktree, scoped via
  `dataclasses.replace`): call `teardown` in a `finally` so each scoped `workdir` reaps its own
  process instead of leaking one. Best-effort + idempotent; no change to existing callers (the
  ACP runtime owns its own client separately and is unaffected).

### Fixed
- **ACP runtime: agent now uses protoAgent's operator tools, not its own** — the persona file
  directs the coding agent to use the `protoagent-operator` tools (`beads_create`, `memory_*`,
  `notes_*`, `set_goal`, …) for anything that must persist, instead of its ephemeral built-in
  todo/memory tools. Verified: 'create a task' now lands a bead in protoAgent, not the agent's
  private session.
- **ACP runtime: request-metadata scope cross-context reset** — an ACP turn awaits across
  context boundaries (the client's reader-loop tasks), so the ADR-0032 `request_metadata_scope`
  token could be reset in a different Context (`ValueError`). The scope now swallows that and
  clears the value instead — no traceback on ACP turns.
- **Instance-scoped config** (ADR 0004) — with `PROTOAGENT_INSTANCE` set, the live config +
  secrets + setup-marker are now per-instance (seeded from the default's on first boot), so a
  scoped instance's saves no longer mutate the shared config. No-op for the default instance.

### Removed
- **`code_with` tool + the `coding_agent` plugin** (breaking) — retired in favour of `delegate_to`
  with an `acp` delegate (ADR 0025), which does the same over one tool alongside a2a/openai
  delegates and a console panel. `plugins/coding_agent/` remains as the **shared ACP client
  library** (`AcpClient`, `_client_for`, `_make_permission`, `evict_client`) that the `delegates`
  plugin and the ACP runtime import — but it no longer ships a manifest/tool, and the
  `coding_agent:` config section is gone. **Migration:** replace `plugins.enabled: [coding_agent]`
  + the `coding_agent.agents` list with `plugins.enabled: [delegates]` + `acp` delegates (same
  `command`/`args`/`workdir`/`permissions` fields); call `delegate_to(name, task)` instead of
  `code_with(agent, task)`. See [CLI coding agents over ACP](docs/guides/coding-agents.md).

## [0.27.0] - 2026-06-08

### Added
- **ACP runtime wired into the request path** (ADR 0033 slice 4) — with `agent_runtime: acp:<agent>`,
  A2A/chat turns are driven by an external coding agent (proto/codex/claude/…), which reaches
  protoAgent's tools through the operator MCP bus mounted into the ACP session. One stateful ACP
  session per thread. Live-verified end-to-end: proto created + persisted a bead via the bus.
- **ACP agent runtime** (ADR 0033 slice 3) — `agent_runtime: acp:<agent>` lets an external
  coding agent (proto/codex/claude/copilot/opencode) drive the turn over ACP: mounts the operator
  MCP bus (slice 1) into `session/new`, builds the prompt via the context contract (slice 2) —
  cacheable persona prefix sent once, then per-turn deltas — and writes back after. Opt-in
  (default `native`, no behavior change); per-agent launch commands are config-overridable.
  Request-path wiring (route live turns + stream to A2A) lands next.
- **Runtime context contract** (ADR 0033 slice 2) — `runtime/context.py`: `assemble_context()`
  → `{stable_prefix, volatile_delta}` (a cacheable persona prefix + per-turn retrieved
  knowledge/skills/prior-sessions) + an `after_turn()` write-back hook, so any runtime (native
  or an external ACP brain) produces context the same cache-disciplined way. Reuses
  `build_system_prompt` + the knowledge/skills retrieval; no change to the native loop.
- **Operator tools as an MCP server** (ADR 0033 slice 1) — publish this agent's tools (core +
  plugin, allowlist-gated) as an MCP server via `python -m server.operator_mcp` (stdio or HTTP),
  so any MCP client (Claude Desktop, Cursor) or an ACP runtime can operate the instance. Config:
  `operator_mcp.enabled` + `operator_mcp.tools`. Stores-only boot (no background loops).

### Docs
- **ACP runtime guide** — a dedicated guide page (Run on a coding agent) for driving protoAgent's runtime with proto/codex/claude/copilot/opencode over ACP.
- **ADR 0033** (Proposed) — pluggable agent runtime over ACP: drive the runtime with an external coding agent (proto/codex/claude/copilot/opencode), runtime≠model axis, operator-tools MCP bus, and a cache-disciplined runtime context contract.

## [0.26.0] - 2026-06-08

### Changed
- **Settings decentralized** — settings now live where the thing lives. **Agent** settings
  (model, routing, goal mode, tools) are a Settings tab in the Agent view; **Memory** settings
  a Settings tab in the Knowledge view. The central Settings surface is now just cross-cutting
  tabs — **Overview · Telemetry · Plugins · System** (Telemetry split out of Overview;
  Integrations renamed Plugins). A plugin with its own view owns its settings; a view-less one
  falls back to Settings → Plugins.

### Added
- **Paste-JSON import for MCP servers** — Agent → MCP → Add server has a Paste JSON mode
  that accepts the standard `{"mcpServers": {…}}` blob (Claude-Desktop style), a single
  server object, or our own export, and imports them all at once (hot-reloaded).
- **Add MCP servers from the console** — Agent → MCP has an inline Add-server form (stdio
  command/args, or http/sse URL) plus a per-server remove button; both hot-reload, so the
  server connects (or drops) without a restart.
- **One-click plugin enable/disable** — toggle a plugin straight from the console Plugins
  panel; it edits `plugins.enabled` and hot-reloads, so tools / middleware / MCP servers apply
  immediately (a console view or background surface needs a restart, and the toggle says so).

### Changed
- **Plugins view reorganized into tabs** — **Local** (installed plugins, grouped Loaded →
  Disabled with enable/disable), **Market** (browse the directory + the `protoagent-plugin`
  GitHub topic), and **Download** (install from a git URL).

### Fixed
- **Marketing changelog: clean entries + no staleness** — the marketing changelog had gone
  stale at v0.21 (0.22–0.24 missing). It's now backfilled through v0.25 with **curated,
  user-facing** entries (kept separate from CHANGELOG.md's detailed dev notes). On release,
  `scripts/changelog.py scaffold` drafts a *concise* entry (bullet titles) for a human to
  polish — never the verbose dev bullets — and a CI guard fails if a released version is
  missing from the marketing changelog.

## [0.25.0] - 2026-06-08

### Added
- **Plugin right-rail panels** (ADR 0026) — a plugin console view can set `placement: "right"`
  to render as a right-sidebar panel (alongside Notes/Beads/Goals/Schedule) instead of a
  left-rail surface. Same iframe host; the substrate for moving Notes to a plugin.

### Changed
- **GitHub read tools → the opt-in `github` plugin** — removed from the default tool set
  (not every agent needs GitHub). Ships disabled; enable with `plugins.enabled: [github]`.
  Tools group under "GitHub" in the Tools tab regardless of source.

### Removed
- **`daily_log` tool removed from core** — it was roxy-specific (roxy ships it as a plugin
  now). Logging an event is `memory_ingest` with a domain; eval cases repointed accordingly.

### Changed
- **Tools tab grouped by subsystem** — the Agent → Tools inventory is sectioned
  (General · GitHub · Notes · Memory · Scheduler · Inbox · Beads · Goals · Delegation ·
  Workflows · Plugin · MCP) with per-group counts, instead of a flat wall of ~30; search
  filters across. `/api/tools` returns a `category` per tool.

### Added
- **Pluggable middleware** (ADR 0032) — plugins contribute LangGraph `AgentMiddleware` via
  `register_middleware(factory)` (appended just before message-capture), and per-request A2A
  metadata is exposed to middleware through `current_request_metadata()` (a per-turn contextvar).
  Middleware was the last core extension point that forced a fork to edit core — a per-turn
  directive (e.g. roxy's project-scope banner) is now a ~15-line plugin with zero core edits.

### Fixed
- **Chat tabs open to the right** — a new chat tab is appended (right) instead of prepended.
- **Favicon renders in the browser tab** — the console favicon link was missing
  `type="image/svg+xml"` and used a base-relative href that 404'd at `/app` (no trailing
  slash); now an absolute `%BASE_URL%` path + the type, with the type added to the docs link
  too. Art unchanged (the protoLabs outline mark).
- **Goals no longer leak between agents** — the goal store wasn't instance-scoped, so two
  agents on one machine shared `/sandbox/goals` and collided on shared session ids (e.g. the
  `system:activity` thread used by scheduled turns). Now namespaced by `PROTOAGENT_INSTANCE`
  (ADR 0004), matching the memory/knowledge/scheduler stores.

### Changed
- **Console IA: "Agent" section + editable identity; Knowledge simplified; Settings→Overview**
  — renamed Runtime→**Agent** with tabs **Identity** (edit name + SOUL.md inline, save = hot
  reload) · Tools · MCP · Subagents · **Skills** (moved from Knowledge) · **Middleware**. Knowledge
  is now a single Store panel. The read-only status snapshot + Telemetry moved to a new
  **Settings → Overview** tab.

### Added
- **Scheduler: per-job timezone** — cron jobs can name an IANA timezone (e.g.
  `America/Chicago`); `"0 9 * * *"` then means 9am local, DST-aware, stored as UTC.
  Exposed via `schedule_task(timezone=…)`, the `/api/scheduler/jobs` API, and a timezone
  picker in the console's Schedule modal (recurring jobs). Defaults to UTC; Workstacean
  gets it natively.

### Fixed
- **Scheduler: fix duplicate/runaway scheduled fires** — `message/send` blocks until the
  turn is terminal, so the old 30s fire timeout false-failed any longer turn and re-fired it
  every tick (~30s) — duplicate scheduled turns + Activity spam. Fires now run off the poll
  loop with an in-flight guard (a slow turn fires once, never re-claimed mid-turn), cron rolls
  forward at claim time, and the timeout is generous + configurable (`SCHEDULER_FIRE_TIMEOUT_S`,
  default 600s).

### Changed
- **Plugin view icons: any lucide icon, no allowlist** — a plugin view can name any
  [lucide](https://lucide.dev) icon (PascalCase or kebab-case). A curated common set renders
  instantly; anything else lazy-loads in a separate on-demand chunk, so authors aren't limited
  to a hardcoded list and the main console bundle stays lean.

### Fixed
- **Scheduler: `schedule_task` dedupes identical jobs** — won't create a second active job
  with the same prompt + schedule, so a self-rescheduling loop can't pile up duplicates that
  all fire together (the cause of scheduled-task Activity spam).

### Changed
- **Console IA: Runtime is top-level with tabs; Plugins is its own section** — the dense
  System panel is split into **Runtime → Overview · Tools · MCP · Subagents · Telemetry**
  (a new `/api/tools` endpoint feeds the live tool inventory), and plugins get a dedicated
  **Plugins** rail section (loaded overview + git-URL install/manage, moved out of Settings).
- **Scheduler is a first-class right-rail panel** — moved from Activity → Schedule to the
  right rail (Notes · Beads · Goals · Schedule), one click from chat.

## [0.24.0] - 2026-06-08

### Added
- **Marketing: a /features page** — differentiators deep-dive + a comparison table vs
  Hermes & OpenClaw (bare-bones+extensible+A2A-orchestration vs batteries-included),
  plus the dogfooding story (SpaceTraders / protoTrader / ORBIS-over-A2A). Linked in nav + footer.
- **Headless-mode docs + advertising** — a [Run headless](docs/guides/headless.md) guide
  (UI tiers, the OpenAI-compatible `/v1/chat/completions` API, the A2A endpoint, auth,
  headless `--setup`), a README "Run headless" section, and a marketing feature card —
  surfacing that protoAgent runs API-first (no UI) drivable via OpenAI or A2A.

### Fixed
- **Subagent YAML override now actually applies at runtime** — `subagents.<name>.{enabled,
  tools,max_turns}` was parsed into config but never reached the runtime registry (only the
  status API read it back, so the documented knob silently did nothing). Wired through
  `_apply_config_subagents` (init + reload); `enabled: false` removes the subagent. The
  config-side default now derives from the registry entry (single source of truth) so it
  can't drift — the old hardcoded default was already missing `memory_ingest`.

### Added
- **Per-subagent model override in config** (ADR 0001) — `subagents.<name>.model` pins a
  subagent to a specific model (blank = `routing.aux_model` → main model), so an operator
  can put a heavy-reasoning subagent on the main model while the rest route to a cheaper
  alias — no code. Applied to the runtime registry at build + reload (the resolution path
  in `_run_subagent` already existed); surfaced in the runtime status.
- **Telemetry: export + disk visibility + retention guardrail** —
  `GET /api/telemetry/export` + an **Export CSV** button download every recorded turn;
  the **Runtime** panel now shows on-disk DB sizes (knowledge / telemetry / checkpoint /
  skills); and `telemetry.retention_days` (default **90**) wires the maintenance loop to
  prune turns older than the window so the per-turn store can't grow unbounded (0 = keep
  forever).

### Changed
- **Unified panel headers** — every surface's header (title + kicker + actions) now renders
  through one shared `PanelHeader` component, with a single `.panel-actions` wrapper.
  Consolidated the duplicate `.settings-actions` / `.notes-actions` classes and standardized
  refresh buttons to icon-only. Completes the panel-layout single-source-of-truth pass
  (with `StageSubnav`).
- **Unified panel sub-tabs** — every surface's sub-tab strip now renders through one
  shared `StageSubnav` component, always **above the panel card**. Previously Settings +
  plugin views rendered their tabs *inside* the card (so they read as part of the heading)
  while the rail surfaces rendered them above — now all consistent (single source of truth).
- **Friendlier Schedule tab** — "New schedule" now opens a **modal** that builds the
  schedule for you: a **calendar** picker for one-off (→ ISO datetime), **presets** for
  recurring (hourly / daily / weekdays / weekly + a time picker, → cron), and a raw-cron
  escape hatch — with a live plain-English preview ("every weekday at 9:00 AM"). No
  hand-written cron required. The list now shows each job's schedule in plain English too.

### Added
- **Desktop build CI** — `.github/workflows/desktop-build.yml` builds the macOS desktop
  app (`.dmg` — the Tauri shell + the PyInstaller server sidecar), signs + notarizes it
  with the org Apple Developer ID, and attaches it to the GitHub release on a semver tag.
  Manual dispatch builds an unsigned dev artifact for iteration. Gives the marketing site
  a real download to point at.
- **`register_embedder` hook** (ADR 0031 follow-up) — a plugin can supply an in-process
  embedder (`registry.register_embedder(name, factory→embed_fn)`), selected with
  `knowledge.embedder: "<name>"`, so the built-in hybrid store can embed locally
  (fastembed / sentence-transformers) without the gateway round-trip. Degrade-safe:
  unregistered / None / error falls back to the gateway embedder.

## [0.23.0] - 2026-06-07

### Changed
- **Console: "Playbooks" renamed to "Skills"** — the surface always *was* the skill
  index (`SKILL.md`); the "Playbook" label collided with Workflows. Now labeled Skills,
  with kickers + a "Skills vs Workflows" doc clarifying the distinction (a skill **advises**
  / is retrieved; a workflow **runs** / is executed). `/api/playbooks` route unchanged.

### Added
- **Pluggable knowledge backend** (ADR 0031) — `registry.register_knowledge_store(name,
  factory)` + a `knowledge.backend` config selector let a plugin supply the store
  (pgvector / Qdrant / Chroma / a managed vector DB) instead of the built-in SQLite/FTS5,
  with no core edit. Degrade-safe: an unregistered name / None / a factory error keeps the
  built-in store. A new `KnowledgeBackend` Protocol (`knowledge.backend`) formalizes the
  consumed surface. The embedder stays gateway-routed (model-swappable via `embed_model`).
- **`controller.evaluate_now(session_id)`** (ADR 0030 D2.2) — a plugin can trigger an
  immediate verifier-only goal check from its own state-change path (e.g. right after a
  sale clears), so achievement is caught promptly instead of at the next monitor tick.
  No agent turn, no drive bookkeeping; met → finish (hooks fire). Completes ADR 0030.
- **Monitor goals** (ADR 0030 D1/D2.1/D3) — a goal can be `"mode": "monitor"` for a
  metric an *external* process drives (a background engine, training run, deployment).
  Monitor goals aren't added to the agent continuation loop (no wasted turns), **never
  exhaust** (a long-horizon target is expected to sit unmet across checks), and are
  evaluated **out-of-band** on a cadence (`goal.monitor_interval`, default 60s) — firing
  the ADR-0028 `on_achieved` hook when met. Closes ADR-0028's deferred D6. `drive` goals
  are unchanged. Surfaced by the SpaceTraders fleet fork (a `credits ≥ 1M` goal that
  stormed the drive loop in minutes).
- **Per-goal `no_progress_limit`** (ADR 0030 D4) — a goal can carry its own patience
  (`/goal {"…", "no_progress_limit": N}` or via `set_goal_safe`), overriding the global
  `goal_no_progress_limit` for that one goal. First slice of monitor goals.
- **Generic plugin "Test connection" button** (ADR 0029) — a plugin manifest can
  declare `test: true` and the console renders a Test-connection button for its
  Settings group (POSTs the group's fields to `/api/config/test-<section>`, unset
  secrets falling back to saved config) — no React edit. Telegram + Slack get it via
  the `chat_surface` wirer's test route; Discord keeps its bespoke button.
- **Communication-plugin standard** (ADR 0029) — a `ChatAdapter` contract +
  `register_chat_surface` helper (`graph/plugins/chat_surface.py`) so a chat
  integration only implements transport (connect / receive / send); admin-gating,
  per-conversation threads, agent invoke, reply-chunking, lifecycle + reconnect, and
  the Test route are shared. Ships a **Telegram** plugin (`plugins/telegram`, opt-in)
  as the ~80-line reference — Slack/WhatsApp/etc. follow the same shape. Discord stays
  bespoke (richer extras) and can migrate incrementally.
- **Slack plugin** (`plugins/slack`, opt-in) — a Socket Mode `ChatAdapter` (no public
  URL), proving the standard handles a **websocket** transport as cleanly as Telegram's
  HTTP long-poll. Needs a bot token (xoxb-) + an app-level token (xapp-).
- **Devkit comms scaffold** — `scaffold_plugin(..., with_comms=True)` writes a
  `ChatAdapter` skeleton on the shared wirer, so the agent can stub a new chat
  integration itself.

## [0.22.0] - 2026-06-07

### Changed
- **Plugin console-view icon allowlist widened** (ADR 0026 D4) — the `views[].icon`
  set grew from 9 to ~35 lucide names spanning dashboards, data, comms, dev, AI,
  finance, **space/fleet** (`Rocket`/`Ship`/`Satellite`/`Radar`), and security, so a
  plugin's rail icon fits its domain (unknown names still fall back to a generic glyph).

### Added
- **`set_goal` tool** (ADR 0028) — the lead agent can set its **own** standing goal,
  ground-truthed by a plugin verifier: `set_goal(condition, check, check_args, …)`
  builds a `plugin` verifier and routes through `set_goal_safe`, so the agent
  literally can't open a shell/`eval` goal (those stay operator-only via `/goal`).
  Registered only when goal mode is on; reads the current session at call time.
- **Goal lifecycle hooks** (ADR 0028, PR3) — a plugin can
  `registry.register_goal_hook(on_achieved=…, on_failed=…)` to react when a goal
  reaches a terminal state (achieved → `on_achieved`; exhausted/unachievable →
  `on_failed`), fired from the controller's `_finish`. Push a notification, record a
  finding, or set the next goal — the goal system becomes a self-improving-loop
  building block, not a dead-end status. Sync or async; a raising hook is logged +
  swallowed (never breaks the goal loop). Completes ADR 0028.
- **Safe programmatic goal-set** (ADR 0028, PR2) — `GoalController.set_goal_safe()`
  + `POST /api/goals` let an agent/plugin/REST caller establish a standing goal
  **only** with a `plugin` verifier. `command`/`test`/`ci` (shell) and `data`
  (`eval`) verifiers are refused programmatically — they stay operator-only via
  `/goal` — so a non-operator goal-set can never reach a code-exec sink (D3). The
  REST route 400s a rejected verifier.
- **Plugin-contributed goal verifiers** (ADR 0028, PR1) — a plugin can
  `registry.register_goal_verifier("<name>", fn)` to contribute an in-process goal
  verifier (auto-namespaced `<plugin-id>:<name>`), referenced by a new **`plugin`**
  verifier type: `{"type":"plugin","check":"<id>:<name>","args":{…}}`. `args` are
  declarative data the verifier validates — no shell, no `eval` — so a plugin can
  ground-truth its own domain state without the `command` verifier's shell-out. A
  bad/erroring verifier never marks a goal met. Wired through the loader + re-set on
  config reload. (PR2 will allow setting a `plugin`-verifier goal programmatically.)

## [0.21.0] - 2026-06-07

### Added
- **Plugin Devkit** — `plugins/plugin-devkit`, a featured first-class plugin that
  is both the canonical **full-bundle example** and the **plugin-authoring kit**.
  In one plugin it demonstrates every contribution type — a tool
  (**`scaffold_plugin`**, writes a new plugin skeleton on disk), a subagent
  (**`plugin-architect`**), a bundled **`building-plugins` skill** (the authoring
  contract), a **`design-plugin` workflow** (request → spec), a **console view**,
  and **config/settings**. Enable it (it ships disabled, like `hello`) to let the
  agent build its own plugins. See [Install & publish plugins](docs/guides/plugin-registry.md).
- **Clean plugin delete** (ADR 0027) — `plugin uninstall <id>` now also removes the
  plugin's `plugins.enabled`/`disabled` reference (no more dangling-enabled errors
  on the next restart), on top of the code dir + `plugins.lock` entry. A new
  **`--purge`** flag (CLI) / `?purge=true` (the `DELETE /api/plugins/{id}` route)
  *also* removes the plugin's config section + its secrets (comment-safe via ruamel).
  Config/secrets are kept by default so a reinstall restores settings; pip deps are
  never auto-removed (shared venv) but are reported. Returns a removal report.

## [0.20.0] - 2026-06-07

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
  console installs. PR4 makes a plugin repo a **full bundle**: `register()` already
  contributes tools / subagents / routes / MCP / views, and conventional
  **`skills/`** (SKILL.md) + **`workflows/`** (`*.yaml`) subdirs are now
  auto-discovered (data — no boilerplate; `register_workflow_dir()` for non-standard
  paths), so installing a repo pulls in skills + workflows too. Publish + install
  guide: [`plugin-registry.md`](docs/guides/plugin-registry.md). See
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

