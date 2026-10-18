# Architecture Decision Records

ADRs capture significant architectural decisions — the context, the options
considered, the decision, and its consequences — so the *why* survives the
people who made it.

Format: lightweight [MADR](https://adr.github.io/madr/)-style. One file per
decision, numbered, never deleted (supersede instead).

Decision status is separate from delivery status. Historical context may describe
retired behavior; follow linked guides and references for current procedures.

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
| [0023](./0023-server-decomposition.md) | Decompose server.py: an AppState container + a composition root | Accepted |
| [0024](./0024-spawn-cli-coding-agents-acp.md) | Spawn CLI coding agents over ACP (`code_with`) | Superseded by [0025](./0025-unified-delegate-registry-and-panel.md) (`code_with` removed; ACP lives on as an `acp` delegate) |
| [0025](./0025-unified-delegate-registry-and-panel.md) | Unified delegate registry + hot-swappable management panel | Accepted (complete; PR1–PR4) |
| [0026](./0026-plugin-contributed-console-surfaces.md) | Plugin-contributed console surfaces (rail views + tabs) | Accepted (complete; PR1–PR3) |
| [0027](./0027-install-plugins-from-git-url.md) | Install plugins from a git URL (shareable plugin repos) | Accepted (sliced) |
| [0028](./0028-plugin-goal-verifiers.md) | Plugin-contributed goal verifiers (+ safe programmatic goals) | Accepted (sliced) |
| [0029](./0029-communication-plugins-standard.md) | A standard for communication (chat-surface) plugins | Accepted |
| [0030](./0030-monitor-goals.md) | Monitor goals: cadence-evaluated, hook-reactive objectives (+ per-goal no-progress limit) | Superseded by [0067](./0067-standalone-watch-primitive.md) |
| [0031](./0031-pluggable-knowledge-backend.md) | Pluggable knowledge backend | Accepted |
| [0032](./0032-pluggable-middleware.md) | Pluggable middleware | Accepted |
| [0033](./0033-pluggable-agent-runtime-acp.md) | Pluggable agent runtime (ACP executor) | Accepted |
| [0034](./0034-plugin-ui-first-class-react.md) | Plugin UI as first-class React (Module Federation) | Superseded by 0038 |
| [0035](./0035-console-layout-dual-rail-mobile-first.md) | Console layout: symmetric dual-rail, mobile-first, persisted UI state | Accepted |
| [0036](./0036-context-menu-system.md) | A unified context-menu system (+ plugin-contributed items) | Accepted |
| [0037](./0037-design-system-foundation.md) | Design-system foundation: Tailwind + `@protolabsai/design` + shadcn/Radix | Accepted |
| [0038](./0038-generative-ui-artifacts-two-mode.md) | Generative-UI artifacts + a two-mode plugin UI (retire federation) | Accepted |
| [0039](./0039-plugin-event-bus.md) | A plugin event bus (decoupled pub/sub, no cross-plugin dependencies) | Accepted |
| [0040](./0040-plugin-bundles.md) | Plugin bundles (install a curated set of plugins as one) | Accepted |
| [0041](./0041-workspaces-and-tiered-stores.md) | Workspaces & tiered stores (the fleet-on-one-host model) | Accepted (v0.31.0) |
| [0042](./0042-fleet-supervisor-unified-console.md) | Fleet supervisor & unified console (background agents, in-place switching) | Accepted (v0.31.0) |
| [0043](./0043-plugin-consumption-sdk-workflows-extraction.md) | Plugin consumption SDK + workflows as the first opt-in core extraction | Accepted |
| [0044](./0044-plugin-driven-console-navigation.md) | Plugin-driven console navigation | Proposed |
| [0045](./0045-chat-panel-slot.md) | The chat panel is a slot (swappable chat plugins) | Accepted |
| [0046](./0046-pluggable-utility-bar.md) | Pluggable utility bar: declarative widgets + data tickers | Proposed |
| [0047](./0047-layered-settings-cascade.md) | Layered settings cascade (App→Host→Agent, per-field, `Field.scope`) | Accepted; path layout amended by [0065](./0065-two-tier-instance-paths.md) |
| [0048](./0048-settings-ia-two-scope-homes.md) | Settings & console-configuration IA (domain-first, scope-as-badge) | Accepted |
| [0049](./0049-bundle-pin-lifecycle.md) | Bundle pin lifecycle (a pin means "last verified working") | Accepted |
| [0050](./0050-background-subagents-reactive-notifications.md) | Background subagents & reactive task notifications | Accepted |
| [0051](./0051-a2a-realtime-streaming-and-component-rendering.md) | A2A realtime streaming & component rendering | Accepted |
| [0052](./0052-user-facing-skills-slash-commands.md) | User-facing skills (slash commands in chat) | Accepted |
| [0053](./0053-wait-yield-and-resume.md) | `wait`: yield-and-resume instead of busy-polling | Accepted |
| [0054](./0054-dream-distill-curation-subagents.md) | `dream` & `distill`: scheduled self-curation subagents | Accepted |
| [0055](./0055-multi-team-orchestration-federated-boards.md) | Multi-team orchestration: federated boards over A2A (scale-out) | Accepted |
| [0056](./0056-unified-dockable-view-model.md) | Unified dockable-view model (tabs ↔ rails) | Proposed |
| [0057](./0057-command-palette.md) | Command palette (⌘⇧K): plugin-extensible quick command | Accepted |
| [0058](./0058-runtime-plugin-install-frozen-app.md) | Runtime plugin install in the frozen desktop app (Discord as the first external comms plugin) | Accepted |
| [0059](./0059-unified-plugin-manager.md) | Unified plugin manager: in-app directory (Discover) + folded config (Installed) | Accepted |
| [0060](./0060-skill-progressive-disclosure.md) | Skills: progressive disclosure (always-on index + load on demand) | Accepted |
| [0061](./0061-frontend-extension-registries.md) | Frontend extension registries (fork-safe console behavior seams) | Accepted |
| [0062](./0062-full-screen-document-viewer.md) | Full-screen document viewer surface | Accepted |
| [0063](./0063-keybinding-system.md) | Scoped, user-rebindable keybinding system | Accepted |
| [0064](./0064-coder-execution-grounded-code-solve.md) | `coder`: execution-grounded code-solve (the board's verifier-grounded coder) | Accepted |
| [0065](./0065-two-tier-instance-paths.md) | Two-tier instance paths (box / instance) + single resolution rule | Accepted |
| [0066](./0066-goal-trust-operator-channel.md) | Goal trust-gate Phase 2: federation token + operator `/api` channel | Accepted |
| [0067](./0067-standalone-watch-primitive.md) | Standalone `watch` primitive (many concurrent watches) | Accepted |
| [0068](./0068-developer-flags-and-panel.md) | Developer flags & the Developer panel (gating pre-release functionality) | Accepted |
| [0069](./0069-memory-delivery-layer.md) | Memory delivery layer: attributed digest, provenance, trust tiers | Accepted |
| [0070](./0070-background-results-push-resume.md) | Background results: push-resume, indexed reports, disposable workers | Accepted |
| [0071](./0071-plugin-permissions-trust-model.md) | Plugin permissions: a trust-and-consent model, not a runtime sandbox | Accepted |
| [0072](./0072-fleet-seed-team-via-config.md) | Fleet seed: declare a team in config (team-via-config) | Proposed |
| [0073](./0073-goal-completion-contracts.md) | Goal completion contracts | Accepted |
| [0074](./0074-system-lifecycle-events.md) | System lifecycle events | Accepted |
| [0075](./0075-external-interfaces-cli-mcp-api.md) | External interfaces: a first-class `protoagent` CLI + tightened MCP / API / model onboarding | Accepted |
| [0076](./0076-managed-git-acp-delegates.md) | Managed git for `acp` delegates: framework-owned branch/commit/push/PR | Accepted |
| [0077](./0077-adversarial-code-review-workflow.md) | Adversarial code-review workflow + the findings convention | Accepted |
| [0078](./0078-fleet-pr-review-qa-tier.md) | Fleet PR review as a protoAgent tier: harvest Quinn, keep the panel | Accepted |
| [0079](./0079-autonomous-operating-model.md) | Autonomous operating model (goals · tasks · scheduling · watches → one OODA loop) | Accepted |
| [0080](./0080-external-secrets-manager.md) | External secrets manager: env hydration with pluggable providers (Infisical first) | Accepted |
| [0081](./0081-self-authored-persona-edit-soul.md) | Self-authored persona: a guarded `edit_soul` tool | Accepted |
| [0082](./0082-per-chat-acp-runtime.md) | Per-chat ACP runtime: hot-swap a coding agent like a model | Accepted |
| [0083](./0083-cowork-mode-archetype.md) | Cowork mode: a prepackaged knowledge-worker archetype | Accepted |
| [0084](./0084-computer-use-cua-driver.md) | Computer use: an out-of-process driver, and the fence it voids | Accepted |
| [0085](./0085-managed-node-runtime.md) | Managed Node.js runtime, provisioned on demand | Accepted |
| [0086](./0086-chat-first-mobile-shell.md) | Chat-first mobile shell (amends ADR 0035 D6) | Accepted |
| [0087](./0087-device-pairing-and-per-device-tokens.md) | Device pairing: per-device revocable tokens + short-TTL QR codes | Accepted |
| [0088](./0088-per-finding-inline-review-threads.md) | Per-finding inline review threads: finding-granular GitHub-native memory | Proposed |
| [0089](./0089-intra-instance-trust-boundary.md) | Intra-instance trust boundary: the hub authenticates, members trust a fleet service token | Accepted |
| [0090](./0090-goals-drive-in-a-chat-tab.md) | Console-created goals drive in a dedicated chat tab (attach / detach) | Accepted |
| [0091](./0091-agent-snapshot-portability.md) | Agent snapshot portability: a declarative, secret-free bundle (not a raw state dump) | Accepted |
| [0092](./0092-desktop-document-baseline-and-versioned-file-artifacts.md) | Desktop document-generation baseline + versioned file artifacts | Accepted |
| [0093](./0093-pip-less-wheel-installer-frozen-plugins.md) | Pip-less wheel installer for frozen-app plugin deps | Accepted |
| [0094](./0094-managed-python-runtime.md) | Managed Python runtime: execute_code on the frozen desktop app | Accepted |
| [0095](./0095-managed-projects-registry.md) | Managed projects registry: one place to declare a project | Proposed |
| [0096](./0096-self-building-loop.md) | The self-building loop: plugin-devkit closes design → build → test → hot-swap | Proposed |
| [0097](./0097-native-oauth-subscription-providers.md) | Native OAuth-subscription providers (run Claude / ChatGPT on your own plan) | Proposed; implemented; provider configuration amended by [0106](./0106-provider-registry.md) |
| [0098](./0098-process-tree-lifecycle.md) | One process-tree lifecycle: `infra/proc` anchors and kills every child tree | Proposed |
| [0099](./0099-chat-bundle-format.md) | Chat-bundle format v1: a structured, artifact-aware export for the P2 hosted viewer | Proposed |
| [0100](./0100-agent-archetypes.md) | Agent archetypes (starter agent types, ratified) | Accepted |
| [0101](./0101-context-lifecycle-log-surface-pressure.md) | Context lifecycle: log, surface, pressure | Accepted |
| [0102](./0102-the-trajectory-session-log-and-derived-surface.md) | The trajectory: append-only session log + derived surface | Partially implemented; remainder deferred |
| [0103](./0103-programmatic-tool-calls-from-execute-code.md) | Programmatic tool calls: agent tools callable from execute_code | Accepted |
| [0104](./0104-session-turns-read-api.md) | The session turns read API: durable chat history has a server-side reader | Accepted |
| [0105](./0105-fleet-shared-delegates.md) | Fleet-shared delegates: one roster on the box, every member's bench | Accepted |
| [0106](./0106-provider-registry.md) | Providers are a registry; every model reference names its provider | Accepted |
| [0107](./0107-unified-self-improvement-policy.md) | Unified, policy-bounded self-improvement reviews | Proposed |
| [0108](./0108-context-architecture-v2.md) | Context Architecture v2: lifecycle, authority, and projection | Proposed |
| [0109](./0109-project-board-one-board-n-repos.md) | project_board: one board, one store, N repos | Accepted |
| [0110](./0110-plugin-setup-steps.md) | Plugin setup steps: a host-run, operator-only fix behind a setup-gap button | Accepted |
| [0111](./0111-zed-operator-editor-acp-shim.md) | Zed as the operator's editor: ACP agent shim, editor deep-links, operator MCP | Proposed |
| [0112](./0112-console-code-pane.md) | Console code pane: a read-only navigator surface the agent points into | Proposed |
| [0113](./0113-agent-pairing-for-remote-fleet-members.md) | Agent pairing: remote fleet members get a paired, revocable token | Accepted |
| [0114](./0114-console-browser-storage-budget.md) | Console browser storage — transcripts in IndexedDB, a budget, and a full quota never crashes | Proposed |
| [0115](./0115-gateway-inflight-limiter.md) | Model calls share a bounded, priority-ordered in-flight lane per endpoint and model (off by default) | Proposed; implemented (off by default) |
| [0116](./0116-local-first-data-analyst-duckdb-and-vega-lite-charts.md) | A local-first data analyst: DuckDB over files in place, charts as Vega-Lite specs, and a plugin-service call seam | Proposed |
| [0117](./0117-subagent-only-tools.md) | Subagent-only tools: a domain's tools live in the subagent that owns it | Proposed |
| [0118](./0118-interactive-answers-inline-artifacts-and-frame-components.md) | Interactive answers: inline artifacts in chat, streamed previews, a send-to-chat bridge, and frame-rendered plugin components | Accepted |
| [0119](./0119-source-layout-with-stable-imports.md) | Organize runtime source without renaming imports | Accepted |
| [0120](./0120-checkpoint-growth-bounds-and-alerts.md) | Bound checkpoint growth on the write path, and alert on it | Proposed |
| [0121](./0121-device-pairing-over-an-operator-supplied-public-url.md) | Device pairing over an operator-supplied public URL: one home origin, a throttled claim, device-scoped tokens | Accepted (2026-10-18) |
