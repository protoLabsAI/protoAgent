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
| [0015](./0015-discord-ingress-surface.md) | Optional native Discord surface (ingress + outbound) | Accepted (design; impl to follow) |
| [0016](./0016-discord-ui-config.md) | In-app Discord configuration (token, admin list, live connect) | Accepted |
| [0017](./0017-google-ui-config.md) | In-app Google (Gmail + Calendar) connect flow | Accepted |
| [0018](./0018-plugin-surfaces-routes-subagents.md) | Plugins contribute surfaces, routes & subagents | Accepted |
