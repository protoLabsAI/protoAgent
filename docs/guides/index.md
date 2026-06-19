# How-To Guides

Task-oriented procedures, grouped by **domain** (the same order used across Tutorials, Reference, and Explanation). Assumes you already have a running agent — see [Tutorials](/tutorials/) if not (the wizard runs with zero setup).

## Getting started

Fork the template and make it yours.

| Guide | When to read |
|---|---|
| [Fork the template (fast path)](/guides/fork-the-template) | Terse checklist for experienced forkers |
| [Customize & deploy](/guides/customize-and-deploy) | You've evaluated via the wizard and now want to fork, rename, and ship your own image |

## Agent core & runtime

Shape how the agent's loop behaves — standing goals, timers, middleware hooks, the runtime brain.

| Guide | When to read |
|---|---|
| [Goal mode](/guides/goal-mode) | You want the agent to pursue a standing goal across turns, not just answer one-shot |
| [Schedule future work](/guides/scheduler) | You want the agent to defer tasks to itself ("remind me tomorrow", recurring sweeps) — local sqlite or Workstacean-backed |
| [Middleware](/guides/middleware) | You want pre/post hooks on the agent turn (plugin-contributed) |
| [Run on a coding agent (ACP runtime)](/guides/acp-runtime) | You want an external coding agent (proto/Codex/Claude/Copilot/OpenCode) to *be* the runtime brain, with protoAgent as the shell |

## Skills, subagents & workflows

Give the agent reusable, named capabilities and delegates.

| Guide | When to read |
|---|---|
| [Skills (`SKILL.md`)](/guides/skills) | You want to drop in reusable, auto-retrieved skill instructions in the AgentSkills `SKILL.md` format |
| [Add a custom skill (A2A card)](/guides/add-a-skill) | You want A2A callers to dispatch a named capability — a *card* skill, distinct from the `SKILL.md` skills above |
| [Configure subagents](/guides/subagents) | You want specialized delegates beyond the shipped `researcher` |
| [Reusable workflows](/guides/workflows) | You want declarative multi-step recipes (`*.yaml`) the agent can run on demand |

## A2A, fleet & delegates

Connect your agent to other agents and endpoints, and run many of them.

| Guide | When to read |
|---|---|
| [Delegates (agents & endpoints)](/guides/delegates) | You want to manage the agents + endpoints your agent talks to via `delegate_to` (a2a / openai / acp), hot-swappable from the console |
| [Spawn CLI coding agents (ACP)](/guides/coding-agents) | You want the agent to drive a CLI coding agent (e.g. protoCLI) over the Agent Client Protocol |
| [Run a fleet (workspaces, archetypes, supervisor)](/guides/fleet) | You want many named agents on one host — created from archetypes, run in the background, sharing a skills commons |
| [Portfolio (one PM, many team boards)](/guides/portfolio) | You want one PM agent to dispatch work to, and track, several team-agents' project boards across repos — over A2A |

## Tools, MCP & plugins

Add capability without forking — external tools, drop-in packages, channels.

| Guide | When to read |
|---|---|
| [Connect MCP servers](/guides/mcp) | You want to plug external tools into the agent via the Model Context Protocol (stdio / HTTP) |
| [Plugins](/guides/plugins) | You want drop-in packages that add tools, skills, routes, background surfaces, subagents and managed MCP servers without forking (Discord ships this way; Google/Slack install as external plugins) |
| [Building a plugin view](/guides/building-react-plugin-views) | You want a plugin to add its own console surface — a left-rail view (dashboard/chart/editor) or a panel that replaces the built-in chat |
| [Build a communication plugin](/guides/communication-plugins) | You want a new inbound/outbound channel (like Discord) as a plugin surface |
| [Install & publish plugins (git URLs)](/guides/plugin-registry) | You want to install a plugin from a git URL, or publish one as a shareable repo (tools + skills + subagents + workflows + views) |
| [Discord surface](/guides/discord) | You want the agent reachable from Discord (the first-party `discord` plugin) |

## Console & UI

Surface the agent to people — the operator console, or no UI at all.

| Guide | When to read |
|---|---|
| [Operator console (React/Tauri)](/guides/react-tauri-ui) | You want the multi-chat React console and to package it for desktop |
| [Run headless (API + A2A)](/guides/headless) | You want the agent as a service — REST + A2A — with no UI |

## Operate & deploy

Ship it, isolate it, fence it in, and watch it.

| Guide | When to read |
|---|---|
| [Deploy via GHCR](/guides/deploy) | You're ready to ship and want auto-deploy wired up |
| [Releasing](/guides/releasing) | You're cutting a versioned release (semver bump → image → GitHub release) |
| [Run multiple instances](/guides/multi-instance) | You want several scoped agents (data isolation) on one host |
| [Sandboxing & egress](/guides/sandboxing) | You want to fence the filesystem + outbound network |
| [Wire Langfuse + Prometheus](/guides/observability) | You need traces and metrics in production |

## Forks & evals

Build a downstream operator fork, keep it synced, and measure it.

| Guide | When to read |
|---|---|
| [Build an operator fork (Roxy)](/guides/operator-fork) | You're building a portfolio-manager / operator agent on top of the template |
| [Sync a fork from upstream](/guides/upstream-sync) | Your fork needs to pull fixes + features down from the template (merge-not-squash) |
| [Eval your fork](/guides/evals) | You want a baseline pass-rate for the tools / memory / A2A surface in your fork |
