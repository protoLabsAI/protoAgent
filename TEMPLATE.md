# Fork checklist

> **Most of what used to be in this file is now a runtime wizard**
> that runs on first page load. Model, tools, persona, name, auth,
> autostart — all captured without editing code. See
> [first-agent tutorial](./docs/tutorials/first-agent.md).
>
> This checklist is only for forks that want to ship their own
> container image under their own GitHub org — the structural
> changes the wizard can't do. For most of that, the new
> [Customize & deploy](./docs/guides/customize-and-deploy.md)
> guide is the canonical source. This file stays for back-compat.

You clicked "Use this template" (or ran `gh repo create --template`).
Now what?

This is the change list to turn a fresh template clone into a
working agent. Work top-down — later steps assume earlier ones.

> **Customize via config, SOUL.md, plugins, and env — not by editing core
> files.** The fewer tracked files you touch, the cleaner you can pull upstream
> fixes (`git merge upstream/main`). `CHANGELOG.md` is `merge=union`, so it never
> conflicts. The steps below reflect that — in particular, **don't rename the
> internal `protoagent` identifier.**

## 0. Decide on an agent name (set it in config, don't rename)

Pick a short slug (`quinn`, `jon`, `matt`). Set the **user-facing** name in
**config** — `identity.name` in `config/langgraph-config.yaml` (or the setup
wizard) — and it flows to the console brand, window title, agent card, and
system prompt. Persona goes in `config/SOUL.md` (loaded into the prompt).

**Do NOT find-and-replace `protoagent` across the repo.** That internal name is
the logger namespace, the `~/.protoagent` data dir, the `PROTOAGENT_*` env vars,
and the plugin namespace — all internal, never shown to a user. A `sed` rewrites
~120 files and turns every upstream merge into a conflict, for zero functional
gain. Leave it. The only slug that's worth wiring is the **`AGENT_NAME` env var**
(Prometheus prefix / Langfuse tag / A2A `<NAME>_API_KEY`) — an env value, not a
file edit. The Docker image label / GHCR path lives in *your* deploy config.

## 1. Enable the release pipeline (a repo variable, not a workflow edit)

Set the **`RELEASE_ENABLED` repo variable** to `true`:

```bash
gh variable set RELEASE_ENABLED --body true
```

`prepare-release.yml` and `release.yml` gate on it, so you enable releases
without editing the workflow files — and upstream changes to them re-sync
cleanly. Until the variable is set, releases won't fire (intentional).

All workflows must stay on the org-owned runner
(`runs-on: namespace-profile-protolabs-linux`); `checks.yml` runs
`verify-workspace-config` on every PR and fails the build on drift.
See [Customize & deploy](./docs/guides/customize-and-deploy.md) §3b.

## 2. Rewrite the persona

Replace `config/SOUL.md` with your agent's identity. See the file
itself for what works and what doesn't. The LLM reads this at
session start, so it sets the tone of every response.

## 3. Rewrite the system prompt

`graph/prompts.py` has the template system prompt + the subagent
sub-prompt. Rewrite both:

- `build_system_prompt` — lead agent identity, goals, guardrails
- `build_subagent_prompt` — per-subagent delegation prompt

Keep the `<scratch_pad>` / `<output>` protocol block — the A2A
handler's output extraction depends on it.

## 4. Add your real tools

`tools/lg_tools.py` ships with a small keyless starter set so a
fresh clone can demonstrate a real research loop: `current_time`,
`calculator` (safe AST eval — no `eval()`), `web_search` (DuckDuckGo
via `ddgs`), and `fetch_url`.

**Keep / drop / add without editing core:** drop tools you don't want via
config — list them under `tools.disabled` in `config/langgraph-config.yaml`
(live-reloadable). Add your own as a **plugin** (`plugins/<id>/` with a
`register(registry)` — see [Plugins](./docs/guides/plugins.md)). Both keep
upstream re-syncs clean. Editing `get_all_tools()` directly (below) still works,
but it's a core edit that conflicts on every upstream merge.

```python
from langchain_core.tools import tool

@tool
async def my_tool(required_arg: str) -> str:
    """What this tool does, from the LLM's POV. First line matters."""
    ...
    return "result the LLM sees"

def get_all_tools(knowledge_store=None):
    return [my_tool, other_tool, ...]
```

Guidelines that have paid off across the protoLabs fleet:

- Require explicit identifiers on every call. Don't silently
  fall back to env-var defaults for `repo` / `project` / etc. —
  the LLM will forget, and the call will target the wrong
  system.
- Return clear error strings (`"Error: ..."`) instead of raising.
  The LLM reads the string and retries. Exceptions bubble up
  to the A2A handler and surface as 500s.
- `AuditMiddleware` already logs duration + success/failure.
  Domain-specific INFO logs go inside the tool body.

## 5. Configure subagents (optional)

`graph/subagents/config.py` ships with one example, a `researcher`.
Add more by registering `SubagentConfig` instances in
`SUBAGENT_REGISTRY`. Each subagent gets a subset of tools and
its own recursion budget.

If your agent doesn't need the subagent pattern at all, delete
the registry entry and call `create_agent_graph(config,
include_subagents=False)` in `server.py`.

## 6. Rewrite the agent card

`server.py::_build_agent_card` has a placeholder card. Replace:

- `name` and `description` with the agent's real surface
- `skills` — each skill is what A2A callers dispatch to. IDs
  should match what your tools can actually accomplish.
- `capabilities.extensions` — declare any A2A extensions your
  agent implements. `cost-v1` is declared by default because
  the runtime emits it automatically; add `effect-domain-v1`
  if your skills mutate shared state you want Workstacean's
  planner to be aware of.

## 7. Set up the model

The template points at a LiteLLM gateway alias called
`protolabs/reasoning`. Two options:

1. **Add a gateway alias** called `protolabs/<your-name>`
   pointing at whichever model you want, then update
   `config/langgraph-config.yaml::model.name` to match.
2. **Use a direct model name** — set `model.name` to e.g.
   `claude-opus-4-6` or `openai/gpt-4o` and let the gateway
   route directly.

Option 1 is preferred — swapping models becomes a gateway edit
instead of a code change.

## 8. Deploy

The Dockerfile uses a single `COPY . /opt/protoagent/` so new
files don't need Dockerfile updates. The bundled pipeline pushes
`ghcr.io/protolabsai/<image>:latest` on every main merge; point
Watchtower (or your deploy tool of choice) at that tag.

Required runtime env:

- `AGENT_NAME` — slug from step 0
- `OPENAI_API_KEY` (or `LITELLM_MASTER_KEY`) — gateway auth
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` — optional,
  for tracing
- `PUSH_NOTIFICATION_ALLOWED_HOSTS` — comma-separated hosts
  allowed as webhook targets (default blocks private IPs)

## 9. Write tests for your skills

The template ships tests for the shared runtime (A2A handler,
tracing, exception logging). Tests for your skills belong in
your fork. A useful pattern:

- `tests/test_my_tool.py` — unit tests for each tool
- Extend `tests/test_a2a_integration.py` with assertions for
  your declared skills + extensions on the agent card

For end-to-end behaviour testing — "when the operator asks X, does
the right tool actually fire and the right row land in the KB?" —
the template ships an eval harness under `evals/`:

```bash
python -m evals.runner             # against a running agent
python -m evals.runner --category tool
```

See [Eval your fork](./docs/guides/evals.md) for what each case
asserts, how the three assertion channels work, and how to add
cases for your fork's new tools.

## 9b. Scheduler — local sqlite or Workstacean

The bundled scheduler ships three agent tools — `schedule_task`,
`list_schedules`, `cancel_schedule` — backed by either a local
sqlite poller or a Workstacean adapter, selected at startup via env:

```bash
# Default: local sqlite, persists at /sandbox/scheduler/<agent_name>/jobs.db
python server.py

# Workstacean: set both and restart
export WORKSTACEAN_API_BASE=http://your-workstacean:3000
export WORKSTACEAN_API_KEY=...
python server.py
```

Multi-fork safety: every job is namespaced by `AGENT_NAME`, so
spinning up `gina-personal` next to `gina-work` (or any number of
ginas under one Workstacean) doesn't cross-fire prompts. See
[Schedule future work](./docs/guides/scheduler.md) for the full
firing model and integration notes.

## 9a. Understand the skill loop

protoAgent's skill loop lets your agent learn from experience automatically.
After forking, review the skill loop lifecycle:

1. **Emission** — subagents configured with `allow_skill_emission=True` capture
   successful `task()` runs as `SkillV1Artifact` objects stored in the skill
   index (`/sandbox/skills.db`, SQLite + FTS5).
2. **Retrieval** — `KnowledgeMiddleware` injects the top-k most relevant skills
   before each LLM call, so the agent reuses proven workflows.
3. **Curation** — run `python -m graph.skills.curator` periodically (or via
   cron) to deduplicate near-identical skills, apply the 90-day confidence
   half-life decay, and prune stale entries below confidence 0.2.

See [docs/tutorials/skill-loop.md](./docs/tutorials/skill-loop.md) for a
complete end-to-end example with cron setup and audit log inspection.

## 10. Delete this file

Once you've worked through the checklist, delete `TEMPLATE.md`.
Keep `README.md` and rewrite it to describe your specific agent.
