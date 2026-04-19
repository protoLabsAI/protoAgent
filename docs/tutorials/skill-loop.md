# Your agent learns from experience

protoAgent includes a **skill loop** — an end-to-end feedback cycle where your agent
captures successful workflows as reusable skills, retrieves them on future tasks, and
periodically curates them to keep the index high-quality.

This tutorial walks through the full example: a skill is born, the agent reuses it, and
the curator automatically optimises the index over time.

---

## What is a skill?

A **skill-v1 artifact** is a structured record of a successful subagent run:

```json
{
  "id": "a1b2c3d4-...",
  "name": "web research summary",
  "description": "Searches DuckDuckGo, fetches top results, and returns a structured summary.",
  "prompt_template": "Research the following topic and return a structured summary: {topic}",
  "tools_used": ["web_search", "fetch_url"],
  "confidence": 0.95,
  "created_at": "2025-03-01T10:00:00+00:00",
  "last_used": "2025-04-10T14:30:00+00:00"
}
```

Skills are emitted as A2A `DataPart` objects (MIME type
`application/vnd.protolabs.skill-v1+json`) and accumulated in the
skill index at `/sandbox/skills/index.jsonl`.

---

## Step 1 — Emit a skill from a subagent run

Call `task()` with `emit_skill=True` in your subagent configuration to capture the
workflow as a reusable recipe:

```python
# graph/subagents/config.py
from graph.subagents.config import SubagentConfig

RESEARCH_WORKER = SubagentConfig(
    name="research_worker",
    description="Searches and summarises information from the web.",
    system_prompt="You are a research assistant. Use web_search and fetch_url to gather information.",
    tools=["web_search", "fetch_url"],
    max_turns=10,
    allow_skill_emission=True,   # ← enables skill capture on success
)
```

When the worker completes successfully the runtime calls `emit_skill_artifact()` which
stores a `SkillV1Artifact` in the async context.  The A2A handler collects it and writes
it to the skill index.

---

## Step 2 — Agent retrieves relevant skills at runtime

`KnowledgeMiddleware` queries the skill index before each LLM call and injects the
top-k matching skills as context:

```
[Relevant knowledge from previous sessions:]
- [skills] web research summary — searches DuckDuckGo and fetches top results
- [skills] calculator — evaluates arithmetic expressions safely
```

The agent uses these injected skills to choose the best subagent or to reuse an
existing prompt template rather than generating one from scratch.  This means each
successful run makes the next run faster and more accurate.

---

## Step 3 — Understand the end-to-end skill loop

Here is the complete feedback cycle in one diagram:

```
┌──────────────┐   emit_skill=True   ┌──────────────────┐
│  task()      │ ──────────────────▶ │  SkillV1Artifact │
│  (subagent)  │                     │  (emitted as     │
│              │                     │   A2A DataPart)  │
└──────────────┘                     └────────┬─────────┘
                                              │ written to
                                              ▼
                                     ┌──────────────────┐
                                     │  skill index     │
                                     │  (index.jsonl)   │
                                     └────────┬─────────┘
                                              │ queried by
                                              ▼
                                     ┌──────────────────┐
                                     │ KnowledgeMiddle- │
                                     │ ware injects     │
                                     │ top-k skills     │
                                     └────────┬─────────┘
                                              │ context for
                                              ▼
                                     ┌──────────────────┐
                                     │   LLM call       │
                                     │   (smarter each  │
                                     │    run)          │
                                     └──────────────────┘
                                              │ periodically
                                              ▼
                                     ┌──────────────────┐
                                     │  Skill Curator   │
                                     │  (dedup + decay  │
                                     │   + prune)       │
                                     └──────────────────┘
```

---

## Step 4 — Run the skill curator

The curator keeps the index lean and high-quality by running three passes:

| Pass | What it does |
|---|---|
| **Confidence decay** | Halves confidence every 90 days of inactivity — stale skills sink to the bottom |
| **Deduplication** | Clusters similar skills by Jaccard similarity; keeps the highest-confidence copy |
| **Pruning** | Removes skills whose confidence has fallen below 0.2 |

Run the curator manually:

```bash
# Dry-run — compute changes but write nothing
python -m graph.skills.curator --dry-run

# Full run with defaults (/sandbox/skills/index.jsonl → audit.jsonl)
python -m graph.skills.curator

# Custom paths and thresholds
python -m graph.skills.curator \
    --index /sandbox/skills/index.jsonl \
    --audit /sandbox/audit/curator.jsonl \
    --prune-threshold 0.15 \
    --half-life 60
```

See all options:

```bash
python -m graph.skills.curator --help
```

### Schedule with cron

Add the curator to cron so it runs automatically:

```cron
# Run skill curator every Sunday at 02:00
0 2 * * 0  cd /opt/protoagent && python -m graph.skills.curator >> /var/log/curator.log 2>&1
```

---

## Step 5 — Inspect the audit log

After each run the curator appends a structured JSON entry to `audit.jsonl`:

```bash
# Pretty-print the latest audit entry
tail -1 audit.jsonl | python -m json.tool
```

Example output:

```json
{
  "run_id": "c7a3f1b2-...",
  "timestamp": "2025-04-14T02:00:01.123456+00:00",
  "dry_run": false,
  "skills_before": 47,
  "skills_after": 41,
  "decay_applied": [
    {"id": "a1b2...", "days_idle": 91.3, "old": 0.95, "new": 0.474}
  ],
  "deduplicated": [
    {"kept": "b3c4...", "removed": ["d5e6...", "f7a8..."]}
  ],
  "pruned": [
    {"id": "9abc...", "name": "outdated search recipe", "confidence": 0.18}
  ]
}
```

---

## What happens to pruned skills?

Pruned skills are removed from the active index but recorded in `audit.jsonl`.  If you
need to recover a pruned skill, grep the audit log for its `id` or `name`:

```bash
grep 'outdated search recipe' audit.jsonl | python -m json.tool
```

Then restore it by adding the original JSONL entry back to `index.jsonl` with a
refreshed `last_used` timestamp and confidence reset to 1.0.

---

## Next steps

- [Add a skill →](/guides/add-a-skill) — how to manually author skills and add them to the index
- [Subagents →](/guides/subagents) — how to configure workers that emit skills
- [Observability →](/guides/observability) — how to monitor curator runs via audit logs and Prometheus metrics
