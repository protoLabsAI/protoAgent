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

### Lane R1a — attributed digest + untrusted framing (D1 + D2) — `M` — SHIPPED #1581
Files: `graph/middleware/memory.py`, `graph/middleware/knowledge.py`,
`tools/lg_tools.py` (new tool), `tests/`.

- [x] Rewrite `load_prior_sessions` → digest format: header stating
      other-session/background-context semantics + one line per session
      (`session_id`, ISO timestamp, surface, topic = first user message
      truncated ~80 chars, message count). **No assistant text, no verbatim
      bodies.** Keep `max_sessions`/`max_tokens` semantics.
- [x] New `recall_session(session_id)` memory tool (in `_build_memory_tools`):
      returns the persisted summary (reasoning-stripped, as today's loader
      did), errors cleanly on unknown id. Registered alongside `memory_recall`.
- [x] Untrusted-reference envelope in `KnowledgeMiddleware.before_model`
      wrapping digest + hot memory + RAG hits: reference data, possibly stale
      or third-party; never instructions; never "the current conversation".
- [x] Tests: digest format golden, no-verbatim-assistant-text regression,
      recall_session happy/unknown-id, envelope present.

### Lane R1b — identity hygiene (D4) — `S` — SHIPPED #1580 + #1584 (review-caught lock race)
Files: `operator_api/chat_routes.py`, `graph/middleware/memory.py`,
`server/chat.py`, `tests/`.

- [x] `/api/chat` default `session_id="api-default"` → mint a unique
      per-call id when omitted (collision-free with console `chat-` ids).
- [x] `_persist_session`: empty `session_id` → **skip persistence** (log
      warning) instead of pooling into `unknown.json`.
- [x] Unify `a2a:`/`chat:` thread-id prefixes on the two chat paths (pick
      `a2a:`; assess migration/orphaning of existing `chat:*` threads and
      document the call in the PR).
- [x] Tests: no shared default id, no unknown.json write, prefix parity.

### Lane R1c — provenance stamping (D5) — `S`/`M` — SHIPPED #1579
Files: `graph/memory_facts.py`, `graph/conversation_harvest.py`,
`tools/lg_tools.py` (`memory_recall` output), `tests/`.

- [x] Facts: `source=<session_id>` (keep `source_type="extracted"`); harvest
      summaries: `source=<thread_id>` (not just the heading).
- [x] `memory_recall` results cite `source` + `created_at` (+ `namespace`
      when set) per hit.
- [x] Tests: stamped rows round-trip; recall output includes citations.

## Round 2 — after Round 1 merges

### Lane R2a — scope filters + incognito (D3) — `M` — SHIPPED #1592 (backend; review also gated retire-harvest for incognito), console toggle in #1596
Files: `graph/middleware/knowledge.py`, `server/chat.py`,
`operator_api/chat_routes.py`, `graph/config.py` (+ config golden),
`apps/web` (thread toggle) — UI part = draft/no-automerge.

- [x] Namespace-aware auto-inject (digest + RAG filtered to instance/workspace
      scope); config key rides the golden-test flow (#1538 pattern).
- [x] Per-thread incognito flag: no session persistence, no memory injection;
      console thread toggle + A2A metadata passthrough.

### Lane R2b — per-turn injection record (D6) — `M` — SHIPPED #1592 (`observability/injection_log.py`, `GET /api/memory/injections`)
Files: `observability/`, `graph/middleware/knowledge.py` (emit), `tests/`.
Depends on R1a (records digest entries).

- [x] Event per model call: which digest sessions, hot chunks (ids), RAG hits
      (chunk ids) entered context; keyed by session_id + turn.
- [x] Queryable via existing telemetry/observability surface.

### Lane R2c — memory inspector (D7) — `M`/`L` — REST SHIPPED #1590; console surface in #1596 (ready for review, no automerge — UX gate)
Files: `operator_api/` (memory routes), `apps/web` (surface).
Depends on R2b for "injected last turn" panel (can land view/edit/delete of
session summaries + hot memory first).

- [x] REST: list/get/delete session summaries; list/edit/delete hot memory.
- [x] Console surface (rail view): summaries, hot memory, last-turn injection.

## Round 3 — trust + time + evals

### Lane R3a — trust tiers + write visibility (D8) — `M` — SHIPPED #1597 (`knowledge/trust.py`, `inject_min_trust`, `memory.hot_written` bus event, confirm-gate config)
- [x] `source_type` tier ranking; low-trust excluded/down-weighted on
      auto-inject, tier visible in tool recall output.
- [x] Hot-memory writes emit Activity/toast event; optional confirm gate
      (config, default off for single-operator).

### Lane R3b — deterministic staleness (D9) — `M` — SHIPPED #1595
- [x] `invalidated_at` column + supersede-don't-delete for facts; retrieval
      prefers valid+recent; `created_at` surfaced in injected context.
      (Migration follows the `namespace` ALTER TABLE precedent.)

### Lane R3c — memory-regression evals (D10) — `M` — SHIPPED #1594 (review hardened fail-open store verification)
- [x] ADR 0012 harness: knowledge-update probe, abstention probe, poisoning
      replay (ingested doc with embedded instruction never persists/fires).

---

## Status (2026-07-01) + follow-ups

All rounds executed 2026-07-01 (parallel worktree lanes, impl→adversarial-review,
draft-first). Merged: #1577 (ADR) #1579 #1580 #1581 #1584 #1585 #1586 #1587
#1588 #1590 #1592 #1595 #1597 (+ ruleset: docs `build` is now a required check).
#1594 (evals) armed on green. #1596 (console Memory surface + incognito) is
ready for review, **no automerge** — operator local-test gate.

Small backend follow-ups surfaced by the console lane (non-blocking):

- [ ] `GET /api/memory/sessions`: add a machine-sortable `created_at`
      (timestamp can currently be the literal `"unknown"`).
- [ ] Chunk-by-id lookup route so the Injections forensics table can
      link-through hot/RAG chunk ids.
- [ ] Pagination/count on `/api/memory/sessions`.
- [ ] Mixed-thread incognito nuance (per-message flag): a later non-incognito
      turn's summary includes earlier incognito content — console toggle
      stamps every send (done in #1596); revisit if a thread-level server
      guarantee is wanted.

## Non-goals (recorded)

- No memory SDK adoption (Mem0/Zep) — ADR 0069 §6.
- No LLM write-time consolidation/freshness judging.
- No full approval-gate on every memory write (visible-event + optional
  confirm only) while single-operator.
