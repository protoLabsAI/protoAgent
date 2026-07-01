# ADR 0069 — Memory delivery layer: attributed digest, provenance, trust tiers

- **Status:** Accepted (2026-07-01) — implementation phased (see §5)
- **Date:** 2026-07-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, memory, knowledge, middleware, security, console
- **Supersedes / Superseded by:** extends ADR 0021 (agent memory: extract, don't dump)

> ADR 0021 fixed what the agent *stores*. This ADR fixes how memory is
> *delivered* into the prompt and how writes are *controlled and audited*.
> Trigger: a fresh chat thread confidently narrated other threads' history as
> "the conversation so far" — because every turn auto-injects a
> `<prior_sessions>` block of raw pooled session summaries, unlabeled and
> unscoped, alongside hot memory and RAG hits with no trust framing, no
> provenance, and no record of what was injected.

---

## 1. Context & Problem statement

Thread transcripts are correctly isolated (checkpointer keyed `a2a:{session_id}`).
The cross-thread "recollection" comes from the auto-inject layer,
`KnowledgeMiddleware.before_model`, which on every non-goal turn injects:

1. **`<prior_sessions>`** — the 10 newest session summary files (all threads
   pooled: chats, background jobs, A2A, palette), up to 500 chars of *verbatim
   message text* per message, ~2 000 tokens, no attribution, no scope filter
   (`graph/middleware/memory.py:load_prior_sessions`).
2. **Hot memory** — every `domain="hot"` chunk, every turn.
3. **Auto-RAG** — top-k store hits on the last user message; the store also
   receives harvested retired-thread summaries and extracted facts, plus
   operator-ingested web pages / YouTube transcripts.

Problems, in order of severity:

- **Identity confusion (the reported symptom).** Raw other-thread text with no
  labeling reads as "the conversation". This is the pattern ChatGPT's injected
  dossier is criticized for (documented context contamination, loss of
  deliberate context control), and it contradicts ADR 0021's own
  extract-don't-dump philosophy at the delivery layer.
- **Poisoning surface.** Untrusted ingested content (web/YouTube → KB,
  harvested threads, any A2A/OpenAI-compat/Discord consumer) is auto-injected
  with no trust framing. This is OWASP ASI06 "Memory & Context Poisoning" —
  demonstrated in production against ChatGPT (SpAIware), Gemini (delayed tool
  invocation), and Claude Code (npm postinstall → MEMORY.md, fixed v2.1.50 by
  *removing memories from the system prompt*). MINJA (arXiv 2503.03704) shows a
  plain *user* of a shared agent can poison memory at ~98 % injection success.
- **Context-quality cost.** Context-rot research (Chroma, 18 frontier models)
  shows degradation well before window limits; distractor studies show
  similar-but-irrelevant content actively misleads. ~2 000 tokens of other
  threads' chatter per turn spends quality, not just tokens.
- **No provenance / no audit.** Facts get `source="harvest"` with no session
  linkage; the `namespace` column is never filtered on auto-inject; nothing
  records *what was injected into which turn*; there is no operator surface for
  cross-session memory (the KB browser shows the store, not the delivery).
- **No temporal validity.** LLMs demonstrably cannot self-adjudicate freshness
  (arXiv 2606.01435); we surface no timestamps and delete/overwrite instead of
  superseding.

## 2. Evidence (what the field converged on)

- **Scoping is the load-bearing safety control.** Claude ships *per-project
  memory isolation* framed explicitly as a safety guardrail; Copilot separates
  repo-facts vs user-preferences namespaces; ChatGPT added project-only memory
  after contamination complaints; every product has an incognito/temporary mode
  excluded from memory.
- **Provenance/validation is the second differentiator.** Copilot stores facts
  *with citations re-validated against the current branch before use*; Gemini
  attaches per-fact timestamp + source rationale and gates the whole block
  RESTRICTED-by-default; Claude surfaces recall as tool calls linking source
  chats; Zep/Graphiti traces every fact to source episodes and *invalidates,
  never deletes* (bi-temporal edges).
- **Reduce memory's architectural authority.** The shipped fixes for real
  poisoning attacks: Claude Code v2.1.50 moved user memories out of the system
  prompt; Claude's prompt instructs distrust of memory contents; Cursor
  requires user approval before background-proposed memories persist.
- **Write-time LLM reconciliation is a trap.** Mem0 publicly reversed its
  ADD/UPDATE/DELETE pipeline in 2026 (it "destroyed context"); staleness is
  handled at retrieval with deterministic recency signals. Validates ADR 0021's
  choice to not adopt a memory SDK.
- **Evals exist.** LongMemEval is the credible harness (only one testing
  knowledge-updates + abstention); DMR is saturated; LoCoMo is flawed.

## 3. Decision

Keep ADR 0021's store architecture. Rework delivery + controls in three phases:

### Phase 1 — label, shrink, scope (kills the symptom)

- **D1 — Attributed digest.** Replace `<prior_sessions>` verbatim text with a
  digest: one line per session — `session_id`, timestamp, surface
  (chat/background/a2a), topic (derived from the first user message; no
  assistant text) — under a header that states these are summaries of *other,
  separate sessions*, background context only, never "the current
  conversation". Full summary retrievable on demand via a new
  **`recall_session(session_id)`** tool (reads the session JSON; reasoning
  stripped as today).
- **D2 — Untrusted-reference framing.** Wrap *all* auto-injected memory
  (digest, hot memory, RAG hits) in one envelope stating it is reference data,
  possibly stale, possibly third-party — never instructions to follow.
- **D3 — Scope filters + incognito.** Auto-inject paths filter by `namespace`
  (instance/workspace scope); a per-thread incognito flag skips both memory
  persistence and injection.
- **D4 — Identity hygiene.** No shared-default `session_id`
  (`api-default`), no `unknown.json` pooling (skip persistence instead), unify
  the `a2a:`/`chat:` thread-id prefixes.

### Phase 2 — provenance + inspector (auditability)

- **D5 — Provenance stamping.** Every fact/summary written carries its source
  `session_id` (schema already has `source`/`namespace`/`created_at`);
  `memory_recall` and `recall_session` cite source session + timestamp.
- **D6 — Injection record.** Per-turn observability event recording which
  memory items (digest entries, hot chunks, RAG hits) entered which turn —
  makes "why did it say that?" answerable and poisoning forensics possible.
- **D7 — Memory inspector.** Console surface: view/edit/delete session
  summaries and hot memory; show the last turn's injected memory. A security
  control first (it is how SpAIware-class attacks get *detected*), UX second.

### Phase 3 — trust + time + regression evals

- **D8 — Trust tiers.** Rank `source_type` (operator-written > agent-extracted
  > ingested/web/external); low-trust tiers down-weighted or excluded from
  auto-inject (recallable by tool with the tier visible). Hot-memory writes
  emit a visible event (toast/Activity); optional confirm gate.
- **D9 — Deterministic staleness.** `created_at` surfaced in injected context;
  facts are superseded (`invalidated_at`), not deleted; retrieval prefers
  valid+recent. No LLM freshness judging.
- **D10 — Memory-regression evals.** ADR 0012 harness additions:
  knowledge-update + abstention probes (LongMemEval-style) and a poisoning
  replay (ingest a document carrying an instruction; assert it never persists
  as memory nor fires in a later session).

## 4. Consequences

- The reported symptom disappears: a fresh thread knows *of* other sessions
  (attributed, expandable) without narrating them as its own history.
- Auto-injected context shrinks from ~2 000 tokens of raw chatter to a
  ~10-line digest — context-quality win per the distraction literature.
- Memory becomes attributable end-to-end: store row → source session → turns
  it was injected into. Poisoning gets detection (inspector, injection record)
  and containment (trust tiers, framing) rather than hope.
- Costs: a new tool in the loop (`recall_session`), one more observability
  event stream, console surface work, and a schema migration
  (`invalidated_at`). No new dependencies.
- Config: `memory.max_sessions`/`max_tokens` keep working (digest honors
  both); new keys ride the ADR 0068-era golden-test flow.

## 5. Implementation (phased)

Tracked in `docs/dev/memory-hardening-tasklist.md` (rounds, parallel lanes,
gates). Phase 1 lands first — D1+D2 in one PR, D4 in a parallel PR, D5
(provenance) can start immediately as well; D3 needs server+console plumbing
and joins round 2 with D6/D7. Phase 3 rides round 3.

## 6. Alternatives considered

- **Drop `<prior_sessions>` entirely (pure retrieval, Claude-style).**
  Rejected for now: ambient continuity is genuinely useful for a personal
  agent (it is why ChatGPT memory is popular), and harvest is
  retirement-delayed — the digest preserves recency at ~5 % of the tokens.
  Revisit if the digest still confuses.
- **Adopt Mem0/Zep wholesale.** Still deferred (ADR 0021 §6): the vendor
  benchmark wars cut both ways, Mem0's own reversal shows the field is
  unsettled, and we need delivery/controls — not a new store.
- **LLM-judged consolidation at write time.** Rejected on Mem0's public
  reversal + arXiv 2606.01435: temporal correctness must be deterministic.
- **Approval gate on every memory write (full Cursor model).** Overkill for a
  single-operator agent today; D8 lands the visible-event + optional-confirm
  middle ground and leaves a full gate as config if multi-user surfaces grow.
