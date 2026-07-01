# Memory-hardening tasklist (ADR 0069)

Execution plan for the memory delivery-layer rework — attributed digest,
provenance, trust tiers. Derived from the 2026-07-01 due-diligence pass
(cross-thread recollection investigation + external survey of product/framework
memory systems and the memory-poisoning literature). Decisions D1–D10 live in
[ADR 0069](../adr/0069-memory-delivery-layer.md).

Organized as **rounds of parallel lanes**: lanes within a round touch disjoint
files and run concurrently (worktrees off `origin/main`, one PR per lane);
rounds are sequenced by real dependencies. LOE: `XS` 1–3 lines · `S` <1 h ·
`M` multi-file+tests · `L` design + broad regression.

Every lane runs the PROTO.md gates before PR: `ruff check .`, `lint-imports`,
`python -m pytest tests/ -q`, `python scripts/live_smoke.py` (+ console gates
for UI lanes). Console/UI lanes follow the local-test gate: **draft PR, no
auto-merge**.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[>]` deferred.

---

## Round 1 — three parallel lanes (no cross-deps)

### Lane R1a — attributed digest + untrusted framing (D1 + D2) — `M`
Files: `graph/middleware/memory.py`, `graph/middleware/knowledge.py`,
`tools/lg_tools.py` (new tool), `tests/`.

- [ ] Rewrite `load_prior_sessions` → digest format: header stating
      other-session/background-context semantics + one line per session
      (`session_id`, ISO timestamp, surface, topic = first user message
      truncated ~80 chars, message count). **No assistant text, no verbatim
      bodies.** Keep `max_sessions`/`max_tokens` semantics.
- [ ] New `recall_session(session_id)` memory tool (in `_build_memory_tools`):
      returns the persisted summary (reasoning-stripped, as today's loader
      did), errors cleanly on unknown id. Registered alongside `memory_recall`.
- [ ] Untrusted-reference envelope in `KnowledgeMiddleware.before_model`
      wrapping digest + hot memory + RAG hits: reference data, possibly stale
      or third-party; never instructions; never "the current conversation".
- [ ] Tests: digest format golden, no-verbatim-assistant-text regression,
      recall_session happy/unknown-id, envelope present.

### Lane R1b — identity hygiene (D4) — `S`
Files: `operator_api/chat_routes.py`, `graph/middleware/memory.py`,
`server/chat.py`, `tests/`.

- [ ] `/api/chat` default `session_id="api-default"` → mint a unique
      per-call id when omitted (collision-free with console `chat-` ids).
- [ ] `_persist_session`: empty `session_id` → **skip persistence** (log
      warning) instead of pooling into `unknown.json`.
- [ ] Unify `a2a:`/`chat:` thread-id prefixes on the two chat paths (pick
      `a2a:`; assess migration/orphaning of existing `chat:*` threads and
      document the call in the PR).
- [ ] Tests: no shared default id, no unknown.json write, prefix parity.

### Lane R1c — provenance stamping (D5) — `S`/`M`
Files: `graph/memory_facts.py`, `graph/conversation_harvest.py`,
`tools/lg_tools.py` (`memory_recall` output), `tests/`.

- [ ] Facts: `source=<session_id>` (keep `source_type="extracted"`); harvest
      summaries: `source=<thread_id>` (not just the heading).
- [ ] `memory_recall` results cite `source` + `created_at` (+ `namespace`
      when set) per hit.
- [ ] Tests: stamped rows round-trip; recall output includes citations.

## Round 2 — after Round 1 merges

### Lane R2a — scope filters + incognito (D3) — `M`
Files: `graph/middleware/knowledge.py`, `server/chat.py`,
`operator_api/chat_routes.py`, `graph/config.py` (+ config golden),
`apps/web` (thread toggle) — UI part = draft/no-automerge.

- [ ] Namespace-aware auto-inject (digest + RAG filtered to instance/workspace
      scope); config key rides the golden-test flow (#1538 pattern).
- [ ] Per-thread incognito flag: no session persistence, no memory injection;
      console thread toggle + A2A metadata passthrough.

### Lane R2b — per-turn injection record (D6) — `M`
Files: `observability/`, `graph/middleware/knowledge.py` (emit), `tests/`.
Depends on R1a (records digest entries).

- [ ] Event per model call: which digest sessions, hot chunks (ids), RAG hits
      (chunk ids) entered context; keyed by session_id + turn.
- [ ] Queryable via existing telemetry/observability surface.

### Lane R2c — memory inspector (D7) — `M`/`L` — draft/no-automerge
Files: `operator_api/` (memory routes), `apps/web` (surface).
Depends on R2b for "injected last turn" panel (can land view/edit/delete of
session summaries + hot memory first).

- [ ] REST: list/get/delete session summaries; list/edit/delete hot memory.
- [ ] Console surface (rail view): summaries, hot memory, last-turn injection.

## Round 3 — trust + time + evals

### Lane R3a — trust tiers + write visibility (D8) — `M`
- [ ] `source_type` tier ranking; low-trust excluded/down-weighted on
      auto-inject, tier visible in tool recall output.
- [ ] Hot-memory writes emit Activity/toast event; optional confirm gate
      (config, default off for single-operator).

### Lane R3b — deterministic staleness (D9) — `M`
- [ ] `invalidated_at` column + supersede-don't-delete for facts; retrieval
      prefers valid+recent; `created_at` surfaced in injected context.
      (Migration follows the `namespace` ALTER TABLE precedent.)

### Lane R3c — memory-regression evals (D10) — `M`
- [ ] ADR 0012 harness: knowledge-update probe, abstention probe, poisoning
      replay (ingested doc with embedded instruction never persists/fires).

---

## Non-goals (recorded)

- No memory SDK adoption (Mem0/Zep) — ADR 0069 §6.
- No LLM write-time consolidation/freshness judging.
- No full approval-gate on every memory write (visible-event + optional
  confirm only) while single-operator.
