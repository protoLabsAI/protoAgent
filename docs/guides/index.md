# How-To Guides

Find the task you need below. If you have not installed an agent yet, start with
[Set up your first agent](/tutorials/first-agent).

## Getting started

| Guide | Task |
|---|---|
| [Use the app](/guides/react-tauri-ui) | Chat, inspect progress, and change settings |
| [Connect and change models](/guides/model-connections) | Add a connection, sign in, and choose a model |
| [Work with files and documents](/guides/documents-and-files) | Attach files, create documents, and download results |
| [Enable document creation](/guides/python-runtime) | Install the desktop interpreter and document libraries |
| [Install and manage plugins](/guides/plugin-registry) | Install, configure, update, or remove plugins |
| [Back up and restore data](/guides/backup-and-restore) | Preserve chats and settings, and recover a saved copy |
| [Fix a problem in the app](/guides/troubleshooting) | Resolve connection, tool, plugin, and file-access errors |
| [Export or copy an agent](/guides/agent-snapshots) | Create a fresh agent from a portable recipe |
| [Add documents and media](/guides/ingestion) | Add files, web pages, PDFs, and media to knowledge |

## Agent core & runtime

| Guide | Task |
|---|---|
| [Goal mode](/guides/goal-mode) | Keep working toward a checked outcome |
| [Watches](/guides/watches) | React when an external condition changes |
| [Schedule future work](/guides/scheduler) | Run one-time or recurring tasks later |
| [Middleware](/guides/middleware) | Add hooks around model and tool calls |
| [System lifecycle events](/guides/lifecycle-events) | React to boot, wake, or app activation |
| [Run on a coding agent (ACP runtime)](/guides/acp-runtime) | **Deprecated** — you already run an `acp:*` runtime and need its reference. For new work, hand coding jobs to an [`acp` delegate](/guides/delegates) instead |

## Skills, subagents & workflows

| Guide | Task |
|---|---|
| [Skills (`SKILL.md`)](/guides/skills) | Teach reusable procedures with `SKILL.md` |
| [Advertise a capability (A2A card)](/guides/add-a-skill) | Advertise an existing capability on the A2A card |
| [Configure subagents](/guides/subagents) | Configure specialized workers |
| [Reusable workflows](/guides/workflows) | Run a defined sequence of subagent steps |
| [Verifier-grounded coder (`coder_solve`)](/guides/coder) | Solve a coding task against an executable verifier |

## Knowledge & memory

Load content the agent can recall, and tune how it's retrieved.

| Guide | Task |
|---|---|
| [Ingest documents & media](/guides/ingestion) | Add files, web pages, PDFs, and media to knowledge |
| [Manage memory](/guides/manage-memory) | Inspect, correct, review, and remove saved context |
| [Tune knowledge recall](/guides/knowledge) | Check retrieval and adjust embeddings and context limits |

## A2A, fleet & delegates

Connect your agent to other agents and endpoints, and run many of them.

| Guide | Task |
|---|---|
| [Delegates (agents & endpoints)](/guides/delegates) | Add, test, and manage callable agents and endpoints |
| [Rooms (`@name` group chat)](/guides/rooms) | Address one or several delegates in chat |
| [CLI coding agents over ACP](/guides/coding-agents) | Hand coding work to a CLI agent over ACP |
| [Run a fleet (workspaces, archetypes, supervisor)](/guides/fleet) | Create and manage agents on one host |
| [Fleet deck](/guides/fleet-deck) | Inspect and control the fleet from a terminal |
| [Portfolio (one PM, many team boards)](/guides/portfolio) | Coordinate project boards across team agents |
| [Agent snapshots (export, share, duplicate)](/guides/agent-snapshots) | Export an agent recipe or create a copy |
| [Build out your agent with a coding agent](/guides/build-with-a-coding-agent) | Ship changes through a PM, project board, and coding delegates |

## Tools, MCP & plugins

| Guide | Task |
|---|---|
| [Extend protoAgent](/guides/extend) | Choose settings, skills, MCP, plugins, or a fork |
| [Connect MCP servers](/guides/mcp) | Connect external MCP tools over stdio or HTTP |
| [Build plugins](/guides/plugins) | Build tools, routes, background work, or views |
| [Build a plugin view (quickstart)](/guides/build-a-plugin-view) | Add a working iframe view to a plugin |
| [Building a plugin view](/guides/building-react-plugin-views) | Use the view bridge, chat slot, and event subscriptions |
| [Build a communication plugin](/guides/communication-plugins) | Build a messaging-platform integration |
| [Publish a plugin](/guides/publish-a-plugin) | Package dependencies and list a plugin for other users |
| [Bundles](/guides/bundles) | Manage a pinned plugin set or publish an archetype |
| [Discord surface](/guides/discord) | Receive and answer Discord DMs and mentions |
| [File GitHub issues (`/issue`)](/guides/file-github-issues) | Submit an issue from the console |
| [Friction log](/guides/friction-log) | Triage tooling problems and clear resolved friction |

## Console & UI

| Guide | Task |
|---|---|
| [Use the app](/guides/react-tauri-ui) | Chat, inspect progress, and change settings |
| [Windows desktop app (install & recovery)](/guides/windows-desktop) | Install, update, or recover the Windows app |
| [Enable document creation (desktop)](/guides/python-runtime) | Install the desktop interpreter and document libraries |
| [Command palette (⌘⇧K)](/guides/command-palette) | Jump to surfaces, settings, and commands |
| [Build and test the console](/guides/build-console) | Develop the frontend or package the desktop app |
| [Developer flags](/guides/developer-flags) | Gate unfinished features by release tier |
| [Access from your phone (LAN / Tailscale)](/guides/phone-access) | Open the console from a phone over LAN or Tailscale |
| [Pair devices and agents](/guides/pairing) | Issue and revoke per-device or per-agent tokens |
| [Run headless (API + A2A)](/guides/headless) | Run the agent as an API service |

## Operate & deploy

| Guide | Task |
|---|---|
| [The `protoagent` command (CLI)](/guides/cli) | Install and manage an instance from a terminal |
| [Deploy via GHCR](/guides/deploy) | Publish an image and configure automatic deployment |
| [Deploy in Docker (config-as-code)](/guides/deploy-docker) | Persist a container and apply config seeds |
| [Deploy on Proxmox (reusable LXC template)](/guides/deploy-proxmox) | Create a reusable Docker-in-LXC template |
| [Releasing](/guides/releasing) | Publish a versioned release |
| [Run multiple instances](/guides/multi-instance) | Separate instance config and stores on one machine |
| [Sandboxing & egress](/guides/sandboxing) | Limit filesystem and network access |
| [Expose to the world](/guides/exposing-protoagent) | Expose token-gated A2A routes while keeping the console private |
| [Wire Langfuse + Prometheus](/guides/observability) | Configure traces, metrics, and audit logs |
| [Model concurrency](/guides/model-concurrency) | Limit parallel model calls and inspect queue pressure |
| [Operating a fleet (health, rollout, triage, recovery)](/guides/operating-a-fleet) | Check health, roll out updates, and recover members |

## Forks & evals

Build a downstream operator fork, keep it synced, and measure it.

| Guide | Task |
|---|---|
| [Fork the template](/guides/fork-the-template) | Create a fork using the developer checklist |
| [Customize and deploy](/guides/customize-and-deploy) | Configure a fork and ship an image |
| [Build an operator fork (Roxy)](/guides/operator-fork) | Build a portfolio-manager agent on the template |
| [Sync a fork from upstream](/guides/upstream-sync) | Merge upstream changes into a fork |
| [Eval your fork](/guides/evals) | Measure tool, memory, and protocol behavior |
