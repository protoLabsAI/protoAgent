---
layout: home
hero:
  name: protoAgent
  text: A lean, A2A-native agent you own
  tagline: A small core on LangGraph that grows with git-URL plugins. Run one agent or orchestrate a fleet — from a console, the OpenAI API, or A2A. Local-first, yours to fork.
  actions:
    - theme: brand
      text: Spin up your first agent
      link: /tutorials/first-agent
    - theme: alt
      text: Run headless
      link: /guides/headless

features:
  - title: A2A-native, built for fleets
    details: Every agent is a spec-compliant A2A 1.0 server — agent card, JSON-RPC over /a2a, SSE streaming, tasks/* lifecycle, push. Delegate to other a2a / openai / acp endpoints; run one or orchestrate many.
  - title: cost-v1 + trace propagation
    details: Every terminal task carries cost-v1 (token usage + wall time) on the artifact's metadata map, keyed by extension URI. a2a.trace metadata nests this agent's Langfuse trace under the caller's.
  - title: Free starter tools
    details: DuckDuckGo web search, URL fetch, safe calculator, and IANA-timezone clock — zero API keys, enough to demo a real research loop on a fresh clone.
  - title: A lean core, opt-in plugins
    details: Start small; add tools, skills, subagents, workflows, FastAPI routes, console dashboards, memory backends, and managed MCP servers as git-URL plugins (python -m server plugin install, pinned in plugins.lock) — without touching core. Telegram and the plugin-devkit ship in-tree; Discord, Google (Gmail+Calendar), and more install as official external plugins from the directory.
  - title: Archetypes + real documents
    details: Spin up agents from starter types — Cowork, Project Manager, Design System Engineer, or any bundle that declares one. Cowork's document skills produce real .docx/.xlsx/.pptx/.pdf via execute_code and the one-click managed Python runtime on desktop.
  - title: Release pipeline
    details: Dispatch prepare-release → semver bump PR → merge → tag → GHCR image → GitHub release → Discord embed. Flip the RELEASE_ENABLED repo variable to enable it on a fork.
---

## Documentation Structure

This site follows the [Diátaxis](https://diataxis.fr) framework:

| Section | Purpose | Start here if you… |
|---------|---------|---------------------|
| [**Tutorials**](/tutorials/) | Learning-oriented walkthroughs | Are about to fork protoAgent for the first time |
| [**How-To Guides**](/guides/) | Task-oriented procedures | Need to accomplish a specific change in a fork |
| [**Reference**](/reference/) | Technical descriptions | Need exact details on an API, config key, or extension |
| [**Explanation**](/explanation/) | Understanding-oriented discussion | Want to understand why the template is shaped this way |

## Canonical reference implementation

[protoLabsAI/roxy](https://github.com/protoLabsAI/roxy) is a filled-in fork built on this template — an autonomous ProtoMaker portfolio manager (persona, A2A skills, project registry). When the docs here don't cover something specific, Roxy is the filled-in example to consult.
