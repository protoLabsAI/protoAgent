<p align="center">
  <img src="docs/public/protoagent-banner.png" alt="protoAgent" width="100%">
</p>

# protoAgent

A lean, A2A-native agent on LangGraph — ships a small core, grows with git-URL plugins.
Run one agent or orchestrate a fleet; drive it from a console, the OpenAI-compatible API,
or A2A. Local-first, yours to fork.

It keeps the boring parts — A2A spec handling, cost/extension emission, tracing, the
release pipeline — stable across every agent in the fleet, so forking an agent is close
to a rewrite of `SOUL.md`, `graph/prompts.py`, and `tools/lg_tools.py` and not much else.
You add capability as plugins instead of inheriting a pile of it.

**Canonical reference implementation**: [protoLabsAI/roxy](https://github.com/protoLabsAI/roxy).
Roxy is a filled-in fork — an autonomous ProtoMaker portfolio manager with its
own persona, A2A skills, and project registry — a good example of what a fork
looks like end-to-end.

**Try it in 5 minutes:** clone, `uv sync && uv run python -m server`
(or `pip install -r requirements.txt && python -m server`), open
<http://localhost:7870>, and walk the
setup wizard — no forking, no `sed`, no Docker required to get
your first agent talking. See the [first-agent tutorial](./docs/tutorials/first-agent.md).

**When you're ready to ship your own:** click **"Use this template"**
at the top of the GitHub repo, then follow [Customize &
deploy](./docs/guides/customize-and-deploy.md) for the fork /
rename / release-pipeline wiring.

## What you get out of the box

| Concern | Where it lives | What it does |
|---|---|---|
| A2A server | `server/a2a.py`, `a2a_impl/executor.py` | JSON-RPC 2.0 over `/a2a`, SSE streaming, `tasks/*` lifecycle, push notifications, well-known agent card, dual token-shape parsing |
| Agent runtime | `graph/agent.py`, `server/` | LangGraph `create_agent()` wired to the A2A handler, with streaming token capture for cost-v1 |
| LLM gateway | `graph/llm.py` | OpenAI-compatible client pointed at LiteLLM — swap models by editing the gateway config, not the fork |
| Subagents | `graph/subagents/config.py` | DeerFlow-pattern delegation via a `task()` tool; one worked example ships — a `researcher` (web + memory, plan→search→synthesize→cite) |
| Delegate to other agents | `plugins/delegates/`, `plugins/coding_agent/` | **`delegate_to`** routes a sub-task to another agent or endpoint over **a2a / openai / acp** — a **built-in** registry, managed + hot-swappable from the console (**Workspace settings ▸ Delegates**), with a health prober. The **acp** type spawns a CLI coding agent (e.g. protoCLI) over the Agent Client Protocol. See [Delegates](./docs/guides/delegates.md), [Spawn CLI coding agents](./docs/guides/coding-agents.md), ADR [0024](./docs/adr/0024-spawn-cli-coding-agents-acp.md) / [0025](./docs/adr/0025-unified-delegate-registry-and-panel.md) |
| Starter tools | `tools/lg_tools.py` | Default-on set: 4 keyless general (`current_time`, `calculator` safe AST eval, `web_search` via DuckDuckGo, `fetch_url`) + 2 HITL (`ask_human`, `request_user_input`) + 4 memory + 3 scheduler + 4 beads + inbox/peer (conditional). Note tools come from the on-by-default `notes` plugin; GitHub read tools are the opt-in `github` plugin. Drop any via `tools.disabled`; add via a plugin. See [Starter tools](./docs/reference/starter-tools.md) |
| Knowledge store | `knowledge/store.py`, `knowledge/hybrid_store.py`, `ingestion/` | sqlite + FTS5 keyword search by default; an optional **hybrid** store adds embeddings + RRF fusion, and the **ingestion pipeline** pulls in txt/md/html/pdf/web/YouTube/audio/video sources. One `chunks` table for operator notes and conversation findings. Default-on; turn off with `middleware.knowledge: false` |
| Extensibility | `graph/skills/`, `tools/mcp_tools.py`, `graph/plugins/`, `plugins/` | Opt-in ways to extend a running agent without forking: **`SKILL.md` skills** (AgentSkills format, auto-retrieved), **MCP servers** (external tools over stdio/HTTP), and **plugins** — drop-in packages that add tools, skills, subagents, workflows, FastAPI routes, background surfaces, managed MCP servers, **console rail views**, and their own config/secrets/Settings. Plugins are **installable from a git URL** (`python -m server plugin install <url>`, pinned in `plugins.lock`) and shareable as repos — a repo is a full bundle. The first-party **Telegram** (`plugins/telegram`) integration ships bundled; **Discord**, **Slack**, and **Google** Gmail/Calendar install as external plugins from their own repos. See [Skills](./docs/guides/skills.md), [MCP](./docs/guides/mcp.md), [Plugins](./docs/guides/plugins.md), [Plugin console views](./docs/guides/plugin-views.md), [Install & publish plugins](./docs/guides/plugin-registry.md), ADR [0001](./docs/adr/0001-extensibility-and-plugin-architecture.md) / [0018](./docs/adr/0018-plugin-surfaces-routes-subagents.md) / [0019](./docs/adr/0019-plugin-config-settings-secrets.md) / [0026](./docs/adr/0026-plugin-contributed-console-surfaces.md) / [0027](./docs/adr/0027-install-plugins-from-git-url.md) |
| Scheduler | `scheduler/` | `schedule_task` / `list_schedules` / `cancel_schedule` tools backed by either a bundled sqlite scheduler or a Workstacean adapter (env-selected). Multi-agent-safe — every job is namespaced by `AGENT_NAME`. See [Schedule future work](./docs/guides/scheduler.md) |
| Eval harness | `evals/` | Side-effect-verified A2A test harness — audit log + reply text + KB state. `python -m evals.runner` against a running agent. See [Eval your fork](./docs/guides/evals.md) |
| Tracing | `observability/tracing.py` | Langfuse trace_session with distributed `a2a.trace` propagation and the OTel cross-context-detach filter |
| Observability | `observability/metrics.py`, `observability/audit.py` | Prometheus metrics with per-agent prefix, JSONL audit log with trace IDs |
| Output protocol | `graph/output_format.py` | `<scratch_pad>` / `<output>` parsing so the model can think without it leaking to users |
| UI | `apps/web/` (React console) | React operator console (the default `--ui console` tier + the Tauri desktop app) over the REST/A2A API — live token-by-token streaming, chat continuity across navigation (+ interrupted-stream self-heal), plugin-contributed rail views, and a PWA shell. See [ADR 0010](./docs/adr/0010-headless-setup-and-ui-tiers.md) |
| Release pipeline | `.github/workflows/*.yml` | Autonomous semver bumps, GHCR image push, GitHub release with filtered notes, optional Discord post |

## Quickstart — from zero to chatting in 5 minutes

```bash
# 1. Get the code (no fork needed for a first run)
git clone https://github.com/protoLabsAI/protoAgent.git my-agent
cd my-agent

# 2. Install deps + run — uv (recommended): creates the venv, installs the
#    core deps from pyproject.toml, and runs the server. No env vars required.
uv sync && uv run python -m server          # core, serves the React console (--ui console)
# Already synced? `uv run --no-sync python -m server` skips the re-resolve.

# 2b. Or with pip — `requirements.txt` installs the core runtime:
#   python -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt        # == pip install -e .
#   python -m server

# 3. Open the wizard — pick your endpoint, pick a model, name the
#    agent, pick a persona preset, hit Launch. The console chat appears
#    once setup completes.
open http://localhost:7870
```

[First-agent tutorial](./docs/tutorials/first-agent.md) walks
through every wizard step with screenshots.

Once you're happy and want to ship it as your own image in your
own GHCR: [Customize & deploy](./docs/guides/customize-and-deploy.md).

## Run headless

The web console is optional — protoAgent is an **API-first agent server**. Run it
headless and drive it over HTTP via the **OpenAI-compatible** API, the **A2A** protocol,
or both. Same agent, tools, skills, memory, and goals — no browser.

```bash
python -m server --ui none --host 0.0.0.0   # API + A2A + /metrics, no UI

# OpenAI-compatible — point any OpenAI client at the base URL:
curl localhost:7870/v1/chat/completions -H "Authorization: Bearer $TOKEN" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'

# A2A — the agent card + JSON-RPC endpoint other agents/fleets call:
curl localhost:7870/.well-known/agent-card.json
```

`--ui` tiers: `console` (React + API, default) · `none` (headless). `full` is a
deprecated alias for `console`. See [Run headless](./docs/guides/headless.md).

## Architecture

```
┌──────────────┐     A2A JSON-RPC + SSE      ┌─────────────────┐
│   Consumer   │ ──────────────────────────▶ │  A2A handler    │
│  (any A2A    │                             │  (FastAPI)      │
│   client)    │ ◀──── cost-v1 DataPart ─────│                 │
└──────────────┘                             └────────┬────────┘
                                                      │
                                                      ▼
                                            ┌─────────────────┐
                                            │  graph/agent.py │
                                            │  (LangGraph     │
                                            │   create_agent) │
                                            └────────┬────────┘
                                                      │
                                                      ▼
                                            ┌─────────────────┐
                                            │  LiteLLM        │  ← model selection
                                            │  gateway        │    lives here,
                                            └─────────────────┘    not in code
```

The A2A handler never talks to the LLM directly — it submits a
message to the LangGraph runtime, which owns the tool loop, the
subagent `task()` delegation, and the structured-output protocol.

## Plugins

A plugin is a drop-in package — a repo with a `protoagent.plugin.yaml` manifest — that
extends a **running** agent without forking: tools, `SKILL.md` skills, subagents,
workflows, FastAPI routes, background surfaces, managed MCP servers, **console rail
views**, and its own config / secrets / Settings. Install one from a git URL:

```bash
python -m server plugin install https://github.com/you/your-plugin   # pinned in plugins.lock
python -m server plugin uninstall your-plugin --purge                # removes code, config + secrets
```

**Browse the directory → [agent.protolabs.studio/plugins](https://agent.protolabs.studio/plugins)**

First-party plugins ship in `plugins/` — `delegates` is a built-in, `notes` and `docs`
are on by default, and the rest are opt-in (enable via `plugins.enabled`):

| Plugin | Adds | What it does |
| --- | --- | --- |
| [`delegates`](./plugins/delegates/) | tool · settings | **Built-in** — `delegate_to` over a2a / openai / acp, managed in Workspace ▸ Delegates |
| [`notes`](./plugins/notes/) | tools · view | **On by default** — one shared markdown note the agent and operator both read/write |
| [`docs`](./plugins/docs/) | tools · view · skill | **On by default** — offline search over protoAgent's own docs |
| [`plugin-devkit`](./plugins/plugin-devkit/) | tool · subagent · skill · workflow · view | The authoring kit + reference plugin — the agent can scaffold and build its own plugins |
| [`workflows`](./plugins/workflows/) | tools | Declarative multi-step subagent workflows (DAG recipes) |
| [`telegram`](./plugins/telegram/) | surface | Run the agent as a Telegram bot — the reference [communication plugin](./docs/guides/communication-plugins.md) |
| [`github`](./plugins/github/) | tools | Read-only GitHub tools over the `gh` CLI |
| [`hello`](./plugins/hello/) | tool · skill · view | Minimal example — copy it to start your own |

Integrations like **Discord**, **Slack** (Socket Mode `ChatAdapter`) and **Google**
Gmail/Calendar (managed MCP server with in-app OAuth) install as **external plugins** from
their own repos — see the [plugin directory](https://agent.protolabs.studio/plugins).

**Chat integrations** (Discord, Telegram, Slack, …) share a contract — implement a
small `ChatAdapter` (connect / receive / send) + a manifest and the admin-gating,
per-conversation threads, reply-chunking, lifecycle, and Test button are handled for
you. See [Build a communication plugin](./docs/guides/communication-plugins.md)
([ADR 0029](./docs/adr/0029-communication-plugins-standard.md)).

**Publish your own:** tag your repo with the [`protoagent-plugin`](https://github.com/topics/protoagent-plugin)
GitHub topic, then open a PR adding it to [`plugins.json`](./sites/marketing/data/plugins.json)
to list it on the directory. See [Install & publish plugins](./docs/guides/plugin-registry.md),
[Plugins](./docs/guides/plugins.md), [Console views](./docs/guides/plugin-views.md).

## A2A extensions shipped by default

| URI | Declared on card | Emitted at runtime |
|---|---|---|
| `cost-v1` (`https://proto-labs.ai/a2a/ext/cost-v1`) | Yes | Yes — every terminal task carries a cost-v1 DataPart with token usage + `durationMs` |
| `confidence-v1` (`https://proto-labs.ai/a2a/ext/confidence-v1`) | Yes | When the model self-reports a `<confidence>` tag — a confidence-v1 DataPart with the score (`[0,1]`), optional explanation, and `success` |
| `a2a.trace` propagation | No (it's a protocol convention, not a card extension) | Yes — reads caller's Langfuse trace context from `params.metadata["a2a.trace"]` and nests this agent's trace under it |

Declare additional extensions on the card in
`server/a2a.py::_build_agent_card_proto` when your agent's skills
actually mutate shared state (see `effect-domain-v1` in the
Workstacean docs for when this applies).

## Push notification support

The A2A handler supports both token shapes the spec permits:

```jsonc
// Shape 1 — top-level (what @a2a-js/sdk serialises by default)
{ "url": "https://consumer/callback/abc", "token": "shared-secret" }

// Shape 2 — structured (RFC-8821 AuthenticationInfo)
{
  "url": "https://consumer/callback/abc",
  "authentication": { "schemes": ["Bearer"], "credentials": "shared-secret" }
}
```

Both produce `Authorization: Bearer shared-secret` on outgoing
webhooks. If your fork is getting 401s on callbacks, check which
shape the consumer is sending before changing anything —
the dual-token parser in `a2a_impl/auth.py` reads both and the
test suite covers both.

## Observability

| What | Where | How to use |
|---|---|---|
| Prometheus metrics | `/metrics` | Scrape; metric prefix is `AGENT_NAME_*` (sanitised) |
| JSONL audit log | `/sandbox/audit/audit.jsonl` | `jq` for forensic replay; every entry has `trace_id` |
| Langfuse traces | `LANGFUSE_*` env vars | Trace tag is `AGENT_NAME`, so filter by tag to find this agent's runs |
| Container logs | `docker logs <container>` | INFO is the default — `LOG_LEVEL=DEBUG` for more |

## Release pipeline

The included GitHub Actions pipeline is optional but opinionated.

- **On every merge to `main`** → `docker-publish.yml` builds and
  pushes `ghcr.io/protolabsai/<image>:latest` + `sha-<short>`.
  Watchtower (or similar) can poll `latest` for auto-deploy.
- **To cut a release** → run `prepare-release.yml` manually
  (`workflow_dispatch`, gated on the `RELEASE_ENABLED` repo var, with a
  patch/minor/major bump input). It opens a "chore: release vX.Y.Z" bump
  PR, auto-merges it, and pushes a semver tag. Releases are on-demand, not
  per-merge.
- **When a semver tag lands** → `release.yml` builds and pushes
  the stable semver Docker tags, creates a GitHub release with
  filtered notes, and posts a Discord embed via the shared
  [`protoLabsAI/release-tools`](https://github.com/protoLabsAI/release-tools) Action.
- **On every PR + push** → `checks.yml` runs the gates: ruff + import
  contracts, `pytest`, an A2A live smoke, a web E2E smoke (vitest +
  Playwright), gitleaks, and `verify-workspace-config` (the fleet
  `.beads`/`.automaker`/owned-runner standard), so drift is caught in CI
  rather than mid-run.

All workflows run on the org-owned `namespace-profile-protolabs-linux`
runner. The three release workflows (`docker-publish`, `prepare-release`,
`release`) gate on `github.repository == 'protoLabsAI/<name>'` so they
no-op on clones that haven't updated the owner — avoids surprise releases
on forks. Update the repo check in all three when forking.

## Requirements

- Python 3.11+ (CI runs 3.12)
- Docker (for the bundled deployment)
- A LiteLLM-compatible OpenAI gateway somewhere on the network
  (see `config/langgraph-config.yaml`)
- Optional: Langfuse, Prometheus, Discord webhook

## Skill loop — agents that learn from experience

protoAgent includes an end-to-end **skill loop**. **Human-authored skills**
dropped in as [`SKILL.md`](./docs/guides/skills.md) folders are loaded into the
index and **retrieved automatically** on future tasks, then surfaced as a "skills
loaded" chip in chat. The agent can also **author its own** skills from a proven
workflow via `/distill` (it writes a new `SKILL.md`), and the skill curator
periodically deduplicates, decays, and prunes non-pinned skills.

| Component | Where it lives | What it does |
|---|---|---|
| `SKILL.md` skills | `config/skills/`, `<config>/skills/`, plugins | Human-authored skills (AgentSkills format) loaded into the index on boot (`source=disk`). Also how the agent self-authors skills, via `/distill`. See [Skills](./docs/guides/skills.md) |
| Skill index | `/sandbox/skills.db` (→ `~/.protoagent`) | SQLite (FTS5) store of loaded skills, queried by `KnowledgeMiddleware` |
| Knowledge injection | `graph/middleware/knowledge.py` | Queries index before each LLM call, injects top-k matching skills as a `<learned_skills>` block (surfaced as a "skills loaded" chip in chat) |
| Skill curator | `graph/skills/curator.py` | Periodic agent that deduplicates, decays, and prunes non-pinned skills (disk skills are pinned) |

### Running the curator

```bash
# Dry-run — see what would change without touching the index
python -m graph.skills.curator --dry-run

# Full curation pass (deduplicate, decay, prune; writes an audit trail)
python -m graph.skills.curator
```

The curator applies a **90-day confidence half-life** (confidence halves for
every 90 days a skill goes unused), clusters near-duplicate skills by
similarity and keeps the highest-confidence copy, and prunes any non-pinned
skill whose confidence has fallen below 0.2 (disk `SKILL.md` skills are pinned).

See the [Skills guide](./docs/guides/skills.md) and
[architecture § Skill loop](./docs/explanation/architecture.md) for the details.

## Contributing

This is a template repo — bugs and improvements to the shared
runtime (the `server/` package, `graph/agent.py`, extension
support, release pipeline) land here. Domain-specific agent logic
lives in the fork, not here.
