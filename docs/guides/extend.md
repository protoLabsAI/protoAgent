# Extend protoAgent

protoAgent is a template repo, so the honest answer to "how do I change it" depends on *what* you're
changing:

- **Your agent's persona, prompts, and domain logic** → fork it. That's what a fork is for
  ([Fork the template](/guides/fork-the-template)).
- **A capability** — a tool, an integration, a console surface, a background worker → **write a
  plugin.** No fork, nothing to keep merging, and it installs from a git URL.
- **An untrusted third-party tool** → [connect an MCP server](/guides/mcp). Out-of-process, so it
  doesn't run with the agent's privileges.

This page is the map for the middle one.

## Start here

| | |
|---|---|
| **Never written one** | [Build your first plugin](/tutorials/first-plugin) — twenty minutes to a tool and a console view on a running agent |
| **Want the model first** | [Plugin architecture](/explanation/plugin-architecture) — what's trusted, what's sandboxed, when your code runs |
| **Building something specific** | [Plugins guide](/guides/plugins) — the contract, seam by seam |
| **Need a signature** | [Reference](#reference) — generated from the code, so it's current |

## What a plugin can contribute

Every one of these is a `register_*` call in your plugin's `register(registry)`
([full list](/reference/plugin-registry-api)):

| | |
|---|---|
| **Tools** the agent can call | `register_tool` · [first tool](/tutorials/first-tool) |
| **Console views** in the left rail | `views:` in the manifest · [building a view](/guides/building-react-plugin-views) · [wire protocol](/reference/plugin-view-bridge) |
| **HTTP routes**, including inbound webhooks | `register_router` · [public paths](/guides/plugins#public-paths) |
| **Background surfaces** — a gateway, a poller, an engine loop | `register_surface` · [resuming across reloads](/guides/plugins#surface-resume) |
| **Skills** and **workflows** | `register_skill_dir` · `register_workflow_dir` · [skills](/guides/skills) · [workflows](/guides/workflows) |
| **Subagents** | `register_subagent` · [subagents](/guides/subagents) |
| **Middleware** around the model call | `register_middleware` · [middleware](/guides/middleware) |
| **Chat commands** (`/yours`) | `register_chat_command` · [slash commands](/guides/skills) |
| **Goal & watch verifiers** | `register_goal_verifier` · [goal mode](/guides/goal-mode) · [watches](/guides/watches) |
| **Knowledge backends & embedders** | `register_knowledge_store` · `register_embedder` · [knowledge](/guides/knowledge) |
| **Managed MCP servers** | `register_mcp_server` · [MCP](/guides/mcp) |
| **A chat bot on a messaging platform** | [communication plugins](/guides/communication-plugins) |

Plus its own **config, secrets, and Settings UI** ([ADR 0019](/adr/0019-plugin-config-settings-secrets)),
and **events** other plugins can subscribe to without importing it
([ADR 0039](/adr/0039-plugin-event-bus)).

## Reference

Generated from the source on every build, and CI-gated — a new seam can't ship undocumented.

| Page | Contents |
|---|---|
| [Plugin manifest](/reference/plugin-manifest) | Every `protoagent.plugin.yaml` field — type, default, meaning |
| [Registry API](/reference/plugin-registry-api) | Every `register_*` seam with its signature, plus `registry.host` |
| [Plugin SDK](/reference/plugin-sdk-api) | `graph.sdk` — subagents, knowledge, watches, background jobs, scheduling, metrics |
| [View bridge](/reference/plugin-view-bridge) | The sandboxed-iframe `postMessage` protocol |
| [Event bus topics](/reference/plugin-events) | Every topic core publishes, with payload keys and matching rules |
| [Testkit](/reference/plugin-testkit) | `load_plugin`, `FakeRegistry`, host stubs |
| [Plugin CLI](/reference/plugin-cli) | `python -m server plugin …` |

## Ship it

| | |
|---|---|
| **Test it** | The [testkit](/reference/plugin-testkit) runs your real modules with no protoAgent installed — `plugin new --tests` scaffolds the suite and a CI workflow |
| **Publish it** | [Install & publish plugins](/guides/plugin-registry) — git URLs, pinning, `plugins.lock` |
| **Group several** | [Bundles](/guides/bundles) — one install for a whole capability stack |
| **Extend the console itself** | [Frontend extension registries](/adr/0061-frontend-extension-registries) for fork-level UI work |
