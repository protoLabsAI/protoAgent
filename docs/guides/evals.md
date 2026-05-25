# Eval your fork

The template ships an eval harness under `evals/` so a fresh fork has
a working test suite for its tools, memory, and A2A protocol surface
on day one. Cases assert across three independent channels — audit
log, reply text, and knowledge-store side effects — so a model that
hallucinates a tool result still gets caught.

## When to read this

- You forked the template and want a baseline pass-rate before you
  ship.
- You added a new tool and want to lock in its intent — "when the
  operator says X, fire tool Y".
- You changed a prompt or model and want to measure regression.

## Run the suite

```bash
# Agent running at $EVAL_BASE_URL (default http://localhost:7870)
# with the relevant auth env (A2A_AUTH_TOKEN and/or <AGENT>_API_KEY).

python -m evals.runner
python -m evals.runner --category tool
python -m evals.runner --tasks current_time_intent,daily_log_intent
```

Reports land in `evals/results/run-<ts>.json`. The CLI prints a
pass/fail board; the JSON report carries reply previews and timing
for post-hoc inspection.

## The three assertion channels

```
prompt → A2A → audit log         (1) tools fired with expected outcome
            → reply text         (2) substrings present in reply
            → KB chunks table    (3) side effects landed correctly
```

A case passes only when every configured assertion holds. Most cases
should opt in to channels 1 and 3 — text patterns alone are brittle
to model paraphrasing and miss hallucinated tool results entirely.

### Why side-effect verification beats text-only

A model can produce "Logged: ..." in its reply without actually
calling `daily_log`. Substring matching passes, the DB stays empty,
and the bug ships. Reading `audit.jsonl` and the `chunks` table
afterward catches it.

## The shape of a case

```json
{
  "id": "unique-id",
  "category": "tool",
  "kind": "ask",
  "name": "Asks for arithmetic → calculator",
  "prompt": "How much is 17 times 23, plus 1?",
  "expected_tools": ["calculator"],
  "expected_patterns": ["392"],
  "verify_kb": {
    "find_chunk_containing": "EVAL-MARK-XYZ",
    "domain": "context"
  },
  "setup":    [{"kb_ingest": {"content": "...", "domain": "...", "heading": "..."}}],
  "teardown": [{"kb_delete_by_content": {"contains": "..."}}]
}
```

Three case `kind`s ship:

- `agent_card` — fetch `/.well-known/agent-card.json` and assert on
  the card's name, skill count, and declared extensions.
- `auth_check` — send a request with a deliberately bad bearer and
  assert the server returns the expected status (401 by default).
- `ask` — the main shape. Sends `prompt`, then asserts on tool firing,
  reply patterns, and KB state.

## Prompt rule

**The tool name never appears in the prompt.** Every prompt must be
plausibly typed by a real user. "Use `daily_log` to record..." tests
instruction-following, not tool selection. If the agent needs to
infer the tool from intent, that *is* the test.

## Setup and teardown — start clean every time

Each `ask` case can pre-seed state via `setup` blocks (BFCL's
`initial_config` pattern: direct DB writes the model never sees) and
clean up after itself with `teardown`. The fixture is invisible to
the agent — it discovers the seeded state via tools, exactly as a
real user would.

`teardown` runs even when assertions fail, so case order doesn't
matter and a noisy failure doesn't poison the next run.

Supported setup/teardown step kinds (extend `evals/verify.py` to add
more):

| Step kind | Args | What it does |
|---|---|---|
| `kb_ingest` | `content`, `domain`, `heading?` | Insert a chunk |
| `kb_delete_by_content` | `contains` | Delete chunks where content LIKE `%contains%` |
| `kb_delete_by_heading` | `domain`, `heading` | Delete chunks matching (domain, heading) |

## What forks should test by default

The starter `tasks.json` covers:

- Agent card discovery (name, skill count, `cost-v1` extension)
- Bearer auth gating
- Each shipped tool fires from a plausible operator prompt
- Memory ingest → recall round-trip
- KB-driven middleware injection (no tool call needed)
- A chained two-tool case (`daily_log` then `memory_recall`)

When you add a tool, add at least one case for it. When you add a
skill to the agent card, extend the `card_discovery` case to assert
the new skill is advertised.

## Running in CI

The runner exits non-zero when any case fails, so it drops in cleanly:

```yaml
- name: Boot agent
  run: docker compose up -d agent

- name: Wait for /health
  run: ./scripts/wait-for-it.sh http://localhost:7870/.well-known/agent-card.json

- name: Run evals
  run: python -m evals.runner
  env:
    EVAL_BASE_URL: http://localhost:7870
    A2A_AUTH_TOKEN: ${{ secrets.AGENT_BEARER }}
```

For non-deterministic categories (any `tool` or `chained` case), aim
for an N-of-M majority threshold rather than 100% — the reference
implementation runs 3 attempts and gates at 2 passes for those
categories. Deterministic ones (`a2a-protocol`, `subsystem` with
seeded state) gate at 100%.

## References

- [`evals/README.md`](https://github.com/protoLabsAI/protoAgent/blob/main/evals/README.md) — quick reference for case authors
- Anthropic — [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- BFCL V3 — [Multi-Turn](https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html)
- [ToolSandbox](https://arxiv.org/html/2408.04682v1) — user simulator + milestones / minefields
