# Fork checklist

You clicked "Use this template" (or ran `gh repo create --template`).
Now what?

This is the change list to turn a fresh template clone into a
working agent. Work top-down — later steps assume earlier ones.

## 0. Decide on an agent name

Pick a short slug (`quinn`, `jon`, `matt`). It will end up in:

- `AGENT_NAME` env var at runtime
- Prometheus metric prefix (`<name>_llm_calls_total`, etc.)
- Langfuse trace tag
- Docker image label + GHCR path
- Release pipeline repo guards

The template uses `protoagent` as the placeholder. Do a
case-insensitive find-and-replace of `protoagent` / `protoAgent`
across the repo when forking:

```bash
git grep -li protoagent | xargs sed -i 's/protoagent/<your-name>/g'
git grep -li protoAgent | xargs sed -i 's/protoAgent/<YourName>/g'
```

Review the diff before committing — the replacement hits
Dockerfile paths (`/opt/protoagent`), GHCR image name, workflow
repo guards, and the Gradio UI branding. All of those want the
new name.

## 1. Claim the repo name in workflow guards

Three workflow files have `github.repository == 'protoLabsAI/protoAgent'`
guards so the template itself doesn't trigger releases:

- `.github/workflows/prepare-release.yml`
- `.github/workflows/release.yml`

Change both to your fork's owner/repo. Until you do, releases
won't fire — intentional, not a bug.

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
fresh clone can demonstrate a real research loop: `echo`,
`current_time`, `calculator` (safe AST eval — no `eval()`),
`web_search` (DuckDuckGo via `ddgs`), and `fetch_url`. Keep the
ones you want, drop the rest, and add your own:

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

`graph/subagents/config.py` ships with one placeholder `worker`.
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
`protolabs/agent`. Two options:

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

## 9a. Understand the skill loop

protoAgent's skill loop lets your agent learn from experience automatically.
After forking, review the skill loop lifecycle:

1. **Emission** — subagents configured with `allow_skill_emission=True` capture
   successful `task()` runs as `SkillV1Artifact` objects stored in the skill
   index (`/sandbox/skills/index.jsonl`).
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
