# Add a custom skill

A *skill* is a named capability A2A callers can dispatch to. Skills live on the agent card, not in your Python code. Adding one does not create a new handler — it advertises an existing capability so callers can target it.

## When you need a skill

- You want another agent (via A2A) to invoke a specific mode of behaviour — e.g. `pr_review` vs. `bug_triage`.
- You want Workstacean's planner to rank your agent against a specific goal.
- You want per-skill cost telemetry (cost-v1 samples are keyed by `(agent, skill)`).

You don't need a skill for "things the chat UI does" — the React console chat is already covered by the default dispatch path.

## 1. Declare it in config

Card skills are config-driven ([#570](/reference/configuration#a2a)) — declare them in the `a2a:` section of `config/langgraph-config.yaml`, **not** by editing `server/a2a.py`. Each entry is a spec:

```yaml
a2a:
  skills:
    - id: summarize_pr
      name: Summarize Pull Request
      description: Fetch a GitHub PR and return a three-bullet summary of what it changes and why.
      tags: [github, summarization]
      examples:
        - "summarize https://github.com/protoLabsAI/protoAgent/pull/64"
```

The server merges these (plus any contributed by plugins via `register_a2a_skill`) and falls back to the template's `chat` placeholder when none are set. A plugin can ship card skills the same way — `registry.register_a2a_skill(spec)` — so a distributable extension carries its own advertised capabilities.

**IDs are sticky**. `cost-v1` samples, `effect-domain-v1` declarations, and Workstacean's routing all key off the `id`. Pick one and don't rename later.

## 2. (Optional) Declare an effect

If the skill actually mutates shared state Workstacean cares about (creates a PR, files an issue, updates a board), declare it under `effect-domain-v1`:

```python
"capabilities": {
    "streaming": True,
    "pushNotifications": True,
    "extensions": [
        {"uri": "https://proto-labs.ai/a2a/ext/cost-v1"},
        {
            "uri": "https://proto-labs.ai/a2a/ext/effect-domain-v1",
            "params": {
                "skills": {
                    "file_bug": {
                        "effects": [{
                            "domain": "protomaker_board",
                            "path": "data.backlog_count",
                            "delta": 1,
                            "confidence": 0.9,
                        }],
                    },
                },
            },
        },
    ],
},
```

Only declare effects for skills that actually mutate shared state. Over-declaring confuses the L1 planner into routing your agent for goals it can't move.

## 3. Teach the LLM about the skill

Skill dispatch doesn't add code paths — the A2A handler just forwards the caller's message to the same LangGraph runtime that handles chat. What makes a skill *do something* is that the system prompt teaches the LLM how to behave when it sees a request matching the skill's intent.

Add the skill's behaviour description to your persona file, **`config/SOUL.md`** (the wizard writes it; `graph/prompts.py::build_system_prompt` reads it into the system prompt — you don't edit `prompts.py`):

```markdown
You handle general chat plus these skills:

- **summarize_pr** — when the user sends a GitHub PR URL, fetch the PR
  with `github_get_pr`, then return a three-bullet summary: what changed,
  why, and any risks. Keep each bullet under two sentences.
```

The LLM routes by reading the user's message. Skill IDs on the card exist for callers; skill *behaviour* lives in `SOUL.md`.

## 4. Verify on the card

```bash
curl http://localhost:7870/.well-known/agent-card.json | jq '.skills[] | .id'
# "chat"
# "summarize_pr"
```

And hit it via A2A:

```bash
curl -X POST http://localhost:7870/a2a \
    -H 'Content-Type: application/json' \
    -d '{
      "jsonrpc": "2.0",
      "id": "1",
      "method": "message/send",
      "params": {
        "message": {
          "role": "user",
          "parts": [{"text": "summarize https://github.com/protoLabsAI/protoAgent/pull/64"}]
        },
        "metadata": {"skillHint": "summarize_pr"}
      }
    }'
```

## 5. Test it

Assert your config declares the skill (it flows to the card via `_resolved_skill_specs`):

```python
def test_config_advertises_summarize_pr():
    from graph.config import LangGraphConfig
    cfg = LangGraphConfig.from_yaml("config/langgraph-config.yaml")
    assert "summarize_pr" in {s["id"] for s in cfg.a2a_skills}
```

## Related

- [Agent card reference](/reference/agent-card) — the full card shape
- [Extensions reference](/reference/extensions) — `cost-v1`, `effect-domain-v1`, `a2a.trace`
- [A2A endpoints reference](/reference/a2a-endpoints) — how callers reach skills
