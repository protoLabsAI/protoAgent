# How-To Guides

Task-oriented procedures. Assumes you already have a running agent (see [Tutorials](/tutorials/) if not — the wizard runs with zero setup).

| Guide | When to read |
|---|---|
| [Customize & deploy](/guides/customize-and-deploy) | You've evaluated via the wizard and now want to fork, rename, and ship your own image |
| [Fork checklist (fast path)](/guides/fork-the-template) | Terser version of the above for experienced forkers |
| [Add a custom skill](/guides/add-a-skill) | Your agent does new things and callers need to dispatch to them |
| [Configure subagents](/guides/subagents) | You want specialized delegates beyond the shipped `researcher` |
| [Wire Langfuse + Prometheus](/guides/observability) | You need traces and metrics in production |
| [Eval your fork](/guides/evals) | You want a baseline pass-rate for the tools / memory / A2A surface in your fork |
| [Schedule future work](/guides/scheduler) | You want the agent to defer tasks to itself ("remind me tomorrow", recurring sweeps) — local sqlite or Workstacean-backed |
| [Deploy via GHCR](/guides/deploy) | You're ready to ship and want auto-deploy wired up |
