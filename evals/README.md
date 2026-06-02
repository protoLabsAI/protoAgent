# Evals

Side-effect-verified eval harness. Each case sends a prompt over A2A
to a running agent and asserts on three independent channels:

1. **Audit log** — every expected tool name fires with the expected
   outcome (`AuditMiddleware` writes JSONL to `/sandbox/audit/audit.jsonl`).
2. **Reply text** — case-insensitive substring patterns appear in the
   model's final reply.
3. **Knowledge store side effects** — the right rows actually land in
   the `chunks` table after a memory-writing turn.

A case passes only when every configured assertion holds.

## Quickstart

```bash
# Agent must be running at $EVAL_BASE_URL (default http://localhost:7870).
# Auth: set $A2A_AUTH_TOKEN if bearer is configured, $<AGENT>_API_KEY
# (or $EVAL_API_KEY) if X-API-Key auth is configured. Both are sent
# when both env vars exist.

python -m evals.runner                                 # all cases
python -m evals.runner --category tool                 # one category
python -m evals.runner --tasks current_time,daily_log
python -m evals.runner --base-url http://host:7870
```

Reports land in `evals/results/run-<ts>.json` per run (gitignored), each
tagged with the model under test (auto-detected from `/healthz`,
overridable with `--model-label`).

## Compare models

```bash
# Boot one agent per model, run the suite against each, print a
# model × category matrix. Each model gets its own throwaway --ui none
# instance (PROTOAGENT_MODEL env override + a unique PROTOAGENT_INSTANCE).
python -m evals.sweep --models protolabs/reasoning,protolabs/agent
python -m evals.sweep --models a,b,c --category tool

# Leaderboard + per-model trend across every report on the box.
python -m evals.report

# One before/after diff of two reports.
python -m evals.compare results/run-OLD.json results/run-NEW.json
```

## Categories

| Category | What it covers |
|---|---|
| `a2a-protocol` | Agent card discovery, auth gating |
| `simple` | Direct LLM answers, no tool use |
| `abstention` | Don't reach for a tool when training data is enough |
| `tool` | Single-tool invocations across the starter set |
| `chained` | Multi-step reasoning that calls 2+ tools |
| `subsystem` | KnowledgeMiddleware retrieval, hot-memory injection |
| `goal` | Goal mode: set a goal, trigger the loop, assert the resulting goal state + footer |
| `subagent` | Lead delegates open-ended work (`expected_any_tools`: `task` / `run_workflow`) |
| `workflow` | A recipe runs end-to-end via `/api/workflows/{name}/run`; assert on its output |

## File layout

```
evals/
  client.py     A2A client (message/send + poll, message/stream, agent card, health, workflows, cancel)
  runner.py     CLI runner — print board, write model-tagged JSON report
  verify.py     Audit-log + KB side-effect assertions (incl. any-of-tools), setup/teardown
  judge.py      LLM-judge rubric scorer (verify_rubric) for quality substrings can't check
  sweep.py      Boot one agent per model + run the suite → model × category matrix
  report.py     Aggregate all reports → leaderboard + per-model trend over time
  compare.py    Diff two reports (pass-rate delta, per-category, flips)
  tasks.json    Cases — 15 covering the starter tools end-to-end
  results/      Per-run reports (gitignored)
```

## Adding a case

Append to `tasks.json`:

```json
{
  "id": "unique-id",
  "category": "tool",
  "kind": "ask",
  "name": "Human-readable description",
  "prompt": "What you ask the agent (in real-user voice — never name the tool)",
  "expected_tools": ["tool_name"],
  "expected_patterns": ["substring-that-must-appear"],
  "verify_kb": {
    "find_chunk_containing": "EVAL-MARK-A1B2",
    "domain": "context"
  },
  "setup": [
    {"kb_ingest": {"content": "...", "domain": "context", "heading": "..."}}
  ],
  "teardown": [
    {"kb_delete_by_content": {"contains": "EVAL-MARK-A1B2"}}
  ]
}
```

Use **unique markers** (`EVAL-MARK-XYZ`, `eval-chain-flag-q9`) in
prompts whenever you need a verifier to disambiguate from real
operator data.

### Goal-mode cases (`kind: "goal"`)

Goal cases set a goal in a pinned session, send a trigger turn, then assert
the resulting goal state and reply footer. The goal is cleared before and
after the case.

```json
{
  "id": "goal_achieved",
  "category": "goal",
  "kind": "goal",
  "name": "...",
  "set_goal": {"condition": "...", "verifier": {"type": "command", "command": "true"}},
  "prompt": "Please make progress toward the goal.",
  "expected_goal_status": "achieved",
  "expected_patterns": ["goal achieved"]
}
```

Prefer deterministic `command` verifiers (`"true"` → achieved, `"false"` with
`"max_iterations": 1` → exhausted) so the outcome is independent of model
competence and needs no host file I/O. `expected_goal_status` is checked
against `GET /api/goal/{session}`; `expected_patterns` against the reply.

## Why side-effect verification

When the model hallucinates a tool result (e.g. "Logged: ..." without
actually calling `daily_log`), text-only checks pass while the DB
stays empty. The audit-log + KB queries here catch it.

## Prompt rule

Every prompt must be plausibly typed by a real user. **The tool name
never appears.** If the agent has to infer the tool from intent, that
*is* the test — leaking the tool name into the prompt is testing
instruction-following, not tool selection.

## References

- Anthropic — [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- BFCL V3 — [Multi-Turn](https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html)
- [ToolSandbox](https://arxiv.org/html/2408.04682v1)
