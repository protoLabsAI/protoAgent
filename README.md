# protoAgent

Template repository for building protoLabs A2A agents on LangGraph.

The purpose of this repo is to keep the boring parts — A2A spec
handling, cost/extension emission, tracing, release pipeline —
stable across every agent in the fleet, so forking an agent is
close to a rewrite of `SOUL.md`, `graph/prompts.py`, and
`tools/lg_tools.py` and not much else.

**Canonical reference implementation**: [protoLabsAI/quinn](https://github.com/protoLabsAI/quinn).
Quinn was the first agent built on this template — it's a good
example of what a filled-in fork looks like end-to-end.

**Try it in 5 minutes:** clone, `pip install -r requirements.txt`,
`python server.py`, open <http://localhost:7870>, and walk the
setup wizard — no forking, no `sed`, no Docker required to get
your first agent talking. See the [first-agent tutorial](./docs/tutorials/first-agent.md).

**When you're ready to ship your own:** click **"Use this template"**
at the top of the GitHub repo, then follow [Customize &
deploy](./docs/guides/customize-and-deploy.md) for the fork /
rename / release-pipeline wiring.

## What you get out of the box

| Concern | Where it lives | What it does |
|---|---|---|
| A2A server | `a2a_handler.py` | JSON-RPC 2.0 over `/a2a`, SSE streaming, `tasks/*` lifecycle, push notifications, well-known agent card, dual token-shape parsing |
| Agent runtime | `graph/agent.py`, `server.py` | LangGraph `create_agent()` wired to the A2A handler, with streaming token capture for cost-v1 |
| LLM gateway | `graph/llm.py` | OpenAI-compatible client pointed at LiteLLM — swap models by editing the gateway config, not the fork |
| Subagents | `graph/subagents/config.py` | DeerFlow-pattern delegation via a `task()` tool; one worked example ships — a `researcher` (web + memory, plan→search→synthesize→cite) |
| Starter tools | `tools/lg_tools.py`, `tools/github_tools.py` | Default-on set: 4 keyless general (`current_time`, `calculator` safe AST eval, `web_search` via DuckDuckGo, `fetch_url`) + 2 HITL (`ask_human`, `request_user_input`) + 4 GitHub read tools over the `gh` CLI + 4 notes + 5 memory + 3 scheduler + 4 beads + inbox/peer (conditional). Drop any via `tools.disabled`; add via a plugin. See [Starter tools](./docs/reference/starter-tools.md) |
| Knowledge store | `knowledge/store.py` | sqlite + FTS5 (LIKE fallback). One `chunks` table for operator notes, daily-log entries, and conversation findings. Default-on; turn off with `middleware.knowledge: false` |
| Extensibility | `graph/skills/`, `tools/mcp_tools.py`, `graph/plugins/`, `plugins/` | Opt-in ways to extend a running agent without forking: **`SKILL.md` skills** (AgentSkills format, auto-retrieved), **MCP servers** (external tools over stdio/HTTP), and **plugins** (drop-in packages adding tools, skills, FastAPI routes, background surfaces, subagents, managed MCP servers, and their own config/secrets/Settings). The first-party **Discord** ingress (`plugins/discord`) and **Google** Gmail/Calendar (`plugins/google`) ship as plugins — disable with `plugins.disabled`. See [Skills](./docs/guides/skills.md), [MCP](./docs/guides/mcp.md), [Plugins](./docs/guides/plugins.md), [ADR 0001](./docs/adr/0001-extensibility-and-plugin-architecture.md) / [0018](./docs/adr/0018-plugin-surfaces-routes-subagents.md) / [0019](./docs/adr/0019-plugin-config-settings-secrets.md) |
| Scheduler | `scheduler/` | `schedule_task` / `list_schedules` / `cancel_schedule` tools backed by either a bundled sqlite scheduler or a Workstacean adapter (env-selected). Multi-agent-safe — every job is namespaced by `AGENT_NAME`. See [Schedule future work](./docs/guides/scheduler.md) |
| Eval harness | `evals/` | Side-effect-verified A2A test harness — audit log + reply text + KB state. `python -m evals.runner` against a running agent. See [Eval your fork](./docs/guides/evals.md) |
| Tracing | `tracing.py` | Langfuse trace_session with distributed `a2a.trace` propagation and the OTel cross-context-detach filter |
| Observability | `metrics.py`, `audit.py` | Prometheus metrics with per-agent prefix, JSONL audit log with trace IDs |
| Output protocol | `graph/output_format.py` | `<scratch_pad>` / `<output>` parsing so the model can think without it leaking to users |
| UI | `apps/web/` (React console), `chat_ui.py` (Gradio) | React operator console (the default `--ui console` tier + the Tauri desktop app) over the REST/A2A API; legacy Gradio chat (`--ui full`) with PWA shell. See [ADR 0010](./docs/adr/0010-headless-setup-and-ui-tiers.md) |
| Release pipeline | `.github/workflows/*.yml` | Autonomous semver bumps, GHCR image push, GitHub release with filtered notes, optional Discord post |

## Quickstart — from zero to chatting in 5 minutes

```bash
# 1. Get the code (no fork needed for a first run)
git clone https://github.com/protoLabsAI/protoAgent.git my-agent
cd my-agent

# 2. Install deps into a venv
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Run the server — no env vars required
python server.py

# 4. Open the wizard — pick your endpoint, pick a model, name the
#    agent, pick a persona preset, hit Launch. The chat UI appears
#    on the same page.
open http://localhost:7870
```

[First-agent tutorial](./docs/tutorials/first-agent.md) walks
through every wizard step with screenshots.

Once you're happy and want to ship it as your own image in your
own GHCR: [Customize & deploy](./docs/guides/customize-and-deploy.md).

## Architecture

```
┌──────────────┐     A2A JSON-RPC + SSE      ┌─────────────────┐
│   Consumer   │ ──────────────────────────▶ │  a2a_handler    │
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

## A2A extensions shipped by default

| URI | Declared on card | Emitted at runtime |
|---|---|---|
| `cost-v1` (`https://proto-labs.ai/a2a/ext/cost-v1`) | Yes | Yes — every terminal task carries a cost-v1 DataPart with token usage + `durationMs` |
| `confidence-v1` (`https://proto-labs.ai/a2a/ext/confidence-v1`) | Yes | When the model self-reports a `<confidence>` tag — a confidence-v1 DataPart with the score (`[0,1]`), optional explanation, and `success` |
| `a2a.trace` propagation | No (it's a protocol convention, not a card extension) | Yes — reads caller's Langfuse trace context from `params.metadata["a2a.trace"]` and nests this agent's trace under it |

Declare additional extensions on the card in
`server.py::_build_agent_card` when your agent's skills actually
mutate shared state (see `effect-domain-v1` in the Workstacean
docs for when this applies).

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
`_extract_push_token` in `a2a_handler.py` reads both and the
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
- **When a non-release PR merges** → `prepare-release.yml` opens a
  "chore: release vX.Y.Z" bump PR, auto-merges it, and pushes a
  semver tag.
- **When a semver tag lands** → `release.yml` builds and pushes
  the stable semver Docker tags, creates a GitHub release with
  filtered notes, and posts a Discord embed via the shared
  [`protoLabsAI/release-tools`](https://github.com/protoLabsAI/release-tools) Action.
- **On every PR + push** → `checks.yml` runs `pytest` and
  `verify-workspace-config` (the fleet `.beads`/`.automaker`/owned-runner
  standard), so drift is caught in CI rather than mid-run.

All workflows run on the org-owned `namespace-profile-protolabs-linux`
runner. The three release workflows (`docker-publish`, `prepare-release`,
`release`) gate on `github.repository == 'protoLabsAI/<name>'` so they
no-op on clones that haven't updated the owner — avoids surprise releases
on forks. Update the repo check in all three when forking.

## Requirements

- Python 3.12+
- Docker (for the bundled deployment)
- A LiteLLM-compatible OpenAI gateway somewhere on the network
  (see `config/langgraph-config.yaml`)
- Optional: Langfuse, Prometheus, Discord webhook

## Skill loop — agents that learn from experience

protoAgent includes an end-to-end **skill loop** where the agent **learns from
its own runs** — successful subagent workflows are captured as reusable skills,
retrieved automatically on future tasks, and periodically optimised by the skill
curator. The same index also serves **human-authored skills** dropped in as
[`SKILL.md`](./docs/guides/skills.md) folders, so authored and agent-emitted
skills are retrieved together.

| Component | Where it lives | What it does |
|---|---|---|
| `SKILL.md` skills | `config/skills/`, `<config>/skills/`, plugins | Human-authored skills (AgentSkills format) loaded into the index on boot (`source=disk`). See [Skills](./docs/guides/skills.md) |
| Skill emission | `graph/extensions/skills.py` | Captures `task()` results as `SkillV1Artifact` when `emit_skill=True`, **persisted** to the index (`source=emitted`) |
| Skill index | `/sandbox/skills.db` (→ `~/.protoagent`) | SQLite (FTS5) store of authored + emitted skills, queried by `KnowledgeMiddleware` |
| Knowledge injection | `graph/middleware/knowledge.py` | Queries index before each LLM call, injects top-k matching skills as a `<learned_skills>` block |
| Skill curator | `graph/skills/curator.py` | Periodic agent that deduplicates, decays, and prunes *emitted* skills (disk skills are pinned) |

### Running the curator

```bash
# Dry-run — see what would change without touching the index
python -m graph.skills.curator --dry-run

# Full curation pass (deduplicate, decay, prune; writes an audit trail)
python -m graph.skills.curator
```

The curator applies a **90-day confidence half-life** (confidence halves for
every 90 days a skill goes unused), clusters near-duplicate skills by
similarity and keeps the highest-confidence copy, and prunes any skill whose
confidence has fallen below 0.2.

See [docs/tutorials/skill-loop.md](./docs/tutorials/skill-loop.md) for a
complete end-to-end example and cron setup.

## Contributing

This is a template repo — bugs and improvements to the shared
runtime (`a2a_handler.py`, `graph/agent.py`, extension support,
release pipeline) land here. Domain-specific agent logic lives
in the fork, not here.
