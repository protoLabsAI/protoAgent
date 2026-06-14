# Architecture Decision Records

ADRs capture significant architectural decisions — the context, the options
considered, the decision, and its consequences — so the *why* survives the
people who made it.

Format: lightweight [MADR](https://adr.github.io/madr/)-style. One file per
decision, numbered, never deleted (supersede instead).

| # | Title | Status |
|---|---|---|
| [0001](./0001-extensibility-and-plugin-architecture.md) | Extensibility & Plugin Architecture | Accepted |
| [0002](./0002-reusable-subagent-workflows.md) | Reusable Subagent Workflows | Accepted |
| [0003](./0003-reactive-agent-activity-thread.md) | Reactive Agent: Activity Thread, Event Bus & Inbound Inbox | Accepted |
| [0004](./0004-multi-instance-data-scoping.md) | Multi-Instance Data Scoping | Accepted |
| [0005](./0005-tool-pollution-and-progressive-disclosure.md) | Tool Pollution & Progressive Tool Disclosure | Accepted |
| [0006](./0006-observability-and-the-self-improving-flywheel.md) | Observability & the Self-Improving Flywheel | Accepted |
| [0007](./0007-directory-aware-operator-agent.md) | Directory-Aware Operator Primitives (enabling a "Roxy" fork) | Accepted |
| [0008](./0008-sandboxing-and-openshell.md) | Sandboxing posture & NVIDIA OpenShell | Accepted |
| [0009](./0009-studio-control-stack.md) | The Studio control stack (goals · workflows · subagents · skills) | Accepted |
| [0010](./0010-headless-setup-and-ui-tiers.md) | Headless setup & UI deployment tiers (lighter stack) | Accepted |
| [0011](./0011-deep-research-workflow.md) | Deep-research workflow with adversarial review | Accepted |
| [0012](./0012-eval-strategy-and-model-comparison.md) | Eval strategy: model-tagged tracking & model comparison | Accepted |
| [0013](./0013-console-data-layer-react-query.md) | Console data layer: TanStack Query + Suspense + ErrorBoundary | Accepted |
| [0014](./0014-a2a-1.0-migration.md) | A2A 0.3 → 1.0: adopt `a2a-sdk` + `protolabs-a2a` | Accepted (shipped #453) |
| [0015](./0015-discord-ingress-surface.md) | Optional native Discord surface (ingress + outbound) | Accepted (shipped as `plugins/discord`) |
| [0016](./0016-discord-ui-config.md) | In-app Discord configuration (token, admin list, live connect) | Accepted |
| [0017](./0017-google-ui-config.md) | In-app Google (Gmail + Calendar) connect flow | Accepted |
| [0018](./0018-plugin-surfaces-routes-subagents.md) | Plugins contribute surfaces, routes & subagents | Accepted |
| [0019](./0019-plugin-config-settings-secrets.md) | Plugins contribute config, settings & secrets | Accepted |
| [0020](./0020-console-ia-run-from-chat.md) | Console IA: run from Chat, manage from surfaces | Accepted |
| [0021](./0021-agent-memory-architecture.md) | Agent memory: extract, don't dump | Accepted |
| [0022](./0022-activity-provenance-feed.md) | Activity is a provenance feed, not a second chat | Accepted |
| [0023](./0023-server-decomposition.md) | Decompose server.py: AppState + composition root | Accepted |
| [0024](./0024-spawn-cli-coding-agents-acp.md) | Spawn CLI coding agents over ACP (`code_with`) | Superseded by [0025](./0025-unified-delegate-registry-and-panel.md) (`code_with` removed; ACP lives on as an `acp` delegate) |
| [0025](./0025-unified-delegate-registry-and-panel.md) | Unified delegate registry + hot-swappable panel (`delegate_to`) | Accepted (complete; PR1–PR4) |
| [0026](./0026-plugin-contributed-console-surfaces.md) | Plugin-contributed console surfaces (rail views + tabs) | Accepted (complete; PR1–PR3) |
| [0027](./0027-install-plugins-from-git-url.md) | Install plugins from a git URL (shareable plugin repos) | Accepted (sliced) |
| [0028](./0028-plugin-goal-verifiers.md) | Plugin-contributed goal verifiers (+ safe programmatic goals) | Accepted (sliced) |
| [0029](./0029-communication-plugins-standard.md) | A standard for communication (chat-surface) plugins | Accepted |
| [0030](./0030-monitor-goals.md) | Monitor goals — cadence-evaluated, hook-reactive objectives (closes 0028 D6) | Accepted (sliced) |
| [0031](./0031-pluggable-knowledge-backend.md) | Pluggable knowledge backend (register_knowledge_store + Protocol + selector) | Accepted |
| [0032](./0032-pluggable-middleware.md) | Pluggable middleware (register_middleware + per-request metadata contextvar) | Accepted |
| [0033](./0033-pluggable-agent-runtime-acp.md) | Pluggable agent runtime (ACP executor) — runtime≠model axis, operator-tools MCP bus, context contract + caching | Accepted |
| [0034](./0034-plugin-ui-first-class-react.md) | Plugin UI as first-class React (Module Federation) — `ui: react` remotes + shared singletons + plugin-ui SDK + trust gate; Notes is the reference port | Superseded by 0038 |
| [0035](./0035-console-layout-dual-rail-mobile-first.md) | Console layout — symmetric dual-rail (swappable surfaces), one tab style, mobile-first (bottom quick-bar + hamburger), persisted UI state (Zustand) | Accepted |
| [0036](./0036-context-menu-system.md) | Unified context-menu system (right-click) — Zustand store + `ContextType` registry + one renderer; plugins contribute items via the plugin-ui SDK (trust-gated); replaces ad-hoc affordances | Accepted |
| [0037](./0037-design-system-foundation.md) | Design-system foundation — Tailwind + `@protolabsai/design` preset + shadcn/Radix (themed by brand tokens); supersedes 0035 S5, flips 0036 D5; shared to plugin remotes | Accepted |
| [0038](./0038-generative-ui-artifacts-two-mode.md) | Generative-UI artifacts (sandboxed iframe, à la Claude/Open WebUI) + two-mode plugin UI (iframe-sandbox for untrusted, build-time `src/ext` registry for forks); **retires Module Federation** | Accepted |
| [0039](./0039-plugin-event-bus.md) | Plugin event bus — decoupled topic pub/sub (extends ADR 0003) with in-process + iframe subscribers and a no-cross-plugin-dependency clause (namespaced publish, gated); notification dots as the first consumer | Accepted |
| [0040](./0040-plugin-bundles.md) | Plugin bundles — install a curated set of plugins as one (`plugin install <url>` of a bundle manifest; builds on ADR 0027 git-installable plugins) | Accepted |
| [0041](./0041-workspaces-and-tiered-stores.md) | Workspaces & tiered stores — the fleet-on-one-host model (per-agent workspace dirs + config/data scoping) | Accepted (v0.31.0) |
| [0042](./0042-fleet-supervisor-unified-console.md) | Fleet supervisor & unified console — background agents, slug-routed in-place switching (`/agents/<slug>/*`), per-agent layout/theme/chat | Accepted (v0.31.0) |
| [0043](./0043-plugin-consumption-sdk-workflows-extraction.md) | Plugin **consumption** SDK (`graph/sdk.py` — `run_subagent`/`subagent_types`/`config`) vs the `register_*` contribution API; proven by extracting **workflows** to an opt-in plugin (`enabled:false`, engine taps core via the SDK) with the Studio surface via `src/ext` + `requiresPlugin` | Accepted |
| [0044](./0044-plugin-driven-console-navigation.md) | Plugin-driven console navigation — plugins contribute nav entries/surfaces into the console rails | Proposed |
| [0045](./0045-chat-panel-slot.md) | The chat panel is a **slot** — fork (`src/ext` id `chat`) or plugin (`views: slot: "chat"`) replaces the built-in surface, inheriting the always-mounted contract (#613); the canonical chat-panel protocol is **A2A 1.0 + the protolabs DataParts** (no TS SDK, no AI-SDK core); records the spike's deliberate deferrals | Accepted |
| [0046](./0046-pluggable-utility-bar.md) | Pluggable utility bar — an `AppShell.utilityBar` slot + **declarative** plugin widgets (indicator / ticker / action) rendered natively (not iframes), fed by the event bus (ADR 0039); safe-for-untrusted, overflow to a `Menu` | Proposed |
| [0047](./0047-layered-settings-cascade.md) | Layered settings cascade — per-field App→Host→Agent override (`Field.scope`, git-style), built on the FIELDS single-source; `host-config.yaml` (`scope_leaf`'d) holds box-shared gateway/model/routing/telemetry defaults, agents override freely; remotes stay delegate refs (#839) | Accepted |
| [0048](./0048-settings-ia-two-scope-homes.md) | Settings IA — **scope is the primary axis**: two homes, **Host / App** (box-shared, the host = first agent's inherited defaults) + **Workspace** (the focused agent, folding in its makeup — Identity/SOUL/Tools/MCP/Subagents/Skills/Middleware — plus its agent-scoped knobs). Surfaces ADR 0047's `Field.scope`; dissolves the category tabs + the bolted-on "Host defaults" cross-cut | Proposed |
| [0049](./0049-bundle-pin-lifecycle.md) | Bundle pin lifecycle — a bundle pin means **"last verified working"**: pin release **tags** not raw SHAs (annotated-tag peel fixed in `_ls_remote_sha`), record `verified_against:` core version, and a **verify-and-bump CI loop** (install pin set into a scratch agent → probe every declared view → auto-PR tag bumps) keeps it true; reference template in `examples/bundles/template/` | Accepted |
| [0050](./0050-background-subagents-reactive-notifications.md) | Background subagents & reactive notifications — a delegation marked `run_in_background` runs **detached as a self-POSTed A2A turn** (reusing the scheduler's substrate → durable task store, lifecycle, telemetry for free), tracked in a `notified`-gated registry keyed to the spawning chat session; completion is **drained into that session's next turn** as a `<task-notification>` (exactly-once) + a `background.completed` bus event. The chat stays live instead of blocking on long sub-work. Phase 1 of a 4-phase plan (reactivity/UX/control follow) | Accepted |
