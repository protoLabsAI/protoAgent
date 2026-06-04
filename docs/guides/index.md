# How-To Guides

Task-oriented procedures. Assumes you already have a running agent (see [Tutorials](/tutorials/) if not — the wizard runs with zero setup).

| Guide | When to read |
|---|---|
| [Customize & deploy](/guides/customize-and-deploy) | You've evaluated via the wizard and now want to fork, rename, and ship your own image |
| [Fork checklist (fast path)](/guides/fork-the-template) | Terser version of the above for experienced forkers |
| [Add a custom skill (A2A card)](/guides/add-a-skill) | You want A2A callers to dispatch a named capability — a *card* skill, distinct from the `SKILL.md` skills below |
| [Configure subagents](/guides/subagents) | You want specialized delegates beyond the shipped `researcher` |
| [Skills (`SKILL.md`)](/guides/skills) | You want to drop in reusable, auto-retrieved skill instructions in the AgentSkills `SKILL.md` format |
| [Connect MCP servers](/guides/mcp) | You want to plug external tools into the agent via the Model Context Protocol (stdio / HTTP) |
| [Plugins](/guides/plugins) | You want drop-in packages that add tools, skills, routes, background surfaces, subagents and managed MCP servers without forking (Discord + Google ship this way) |
| [React + Tauri UI migration](/guides/react-tauri-ui) | You want to replace Gradio with the multi-chat React console and package it for desktop |
| [Wire Langfuse + Prometheus](/guides/observability) | You need traces and metrics in production |
| [Eval your fork](/guides/evals) | You want a baseline pass-rate for the tools / memory / A2A surface in your fork |
| [Schedule future work](/guides/scheduler) | You want the agent to defer tasks to itself ("remind me tomorrow", recurring sweeps) — local sqlite or Workstacean-backed |
| [Deploy via GHCR](/guides/deploy) | You're ready to ship and want auto-deploy wired up |
