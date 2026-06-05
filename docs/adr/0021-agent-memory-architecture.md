# ADR 0021 — Agent memory: extract, don't dump

- **Status:** Accepted (2026-06-04) — implementation phased (see §5)
- **Date:** 2026-06-04
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, memory, knowledge, middleware, retrieval
- **Supersedes / Superseded by:** —

> The agent's long-term memory was never designed — it accreted from the
> original template. `MemoryMiddleware` *claims* to "extract key findings" but
> actually dumps every raw assistant turn (scratch_pad and all) into the
> knowledge base, which the retrieval layer then feeds back into future prompts.
> This ADR replaces the duct tape with the standard model: **three memory types,
> extract-don't-dump, background-not-hot-path, and never persist the model's
> internal reasoning.**

---

## 1. Context & Problem statement

Making the Knowledge Store browsable (ADR 0020) exposed that it's full of junk:
raw `<scratch_pad>` reasoning, whole conversation outputs truncated mid-sentence,
and trivia like "READY" — all stored as `finding_type="insight"`.

The cause is `graph/middleware/memory.py`. It ships from the **initial template
commit** and has only ever been patched, never designed — there is **no prior
ADR**. Its docstring says it "extracts key topics/findings"; the implementation
(`after_agent`) stores `last_ai[:2000]` **raw**, every turn ≥100 chars, with no
`extract_output()`. The extraction was never built. Worse, `KnowledgeMiddleware`
injects top-k store matches as context before each turn — so the scratch_pad it
dumped gets **recycled into future prompts**.

Underneath sit **~7 overlapping mechanisms** with no unifying design:

| Mechanism | Stores | Verdict |
|---|---|---|
| `MemoryMiddleware` session-JSON → `<prior_sessions>` | raw messages | leaks reasoning; redundant with the checkpointer |
| `MemoryMiddleware` KB `insight` findings | **raw turns, truncated, scratch_pad** | the junk — remove |
| `KnowledgeIngestMiddleware` (`ingest`) | tool outputs (opt-in) | ok |
| `conversation_harvest` (`conversation`) | **summarized, scratch_pad-stripped** | the *right* pattern |
| `KnowledgeMiddleware` | retrieval/injection | retrieval side |
| `memory_write`/`memory_recall` tools | agent-chosen facts | ok |
| LangGraph checkpointer | full thread history | the real history |

## 2. Evidence (this is a solved problem)

- **Raw-turn storage is the named anti-pattern.** Mem0: ~60–70% of conversation
  tokens are "small talk, repetition, or transient reasoning"; storing verbatim
  causes "memory bloat, degraded retrieval precision, and rising storage costs."
  Their extract → consolidate → retrieve pipeline reports ~90% token savings /
  91% lower p95 vs full context. (arXiv 2504.19413)
- **Three memory types** (LangChain LangMem): **semantic** (discrete facts),
  **episodic** (interaction summaries), **procedural** (skills/instructions). And
  extraction is **background/batch, not hot-path**.
- **Gate + reflect** (Generative Agents, arXiv 2304.03442): score by importance,
  retrieve by recency+importance+relevance, periodically reflect to distil —
  not every observation is kept.

protoAgent violates all three: raw not extracted, undifferentiated, every turn,
on the response path.

## 3. Decision

Adopt the standard model, mapped onto primitives we already have:

- **Semantic memory** (facts) — discrete, LLM-**extracted** facts, **consolidated**
  (dedupe/update, not append). Replaces the raw `insight` dump. Backed by the KB.
- **Episodic memory** (what happened) — `conversation_harvest` already does this
  right (summarize on retirement via the aux model, `extract_output`-stripped).
  It becomes the canonical session memory.
- **Procedural memory** (how) — skills / Playbooks (`skills.db`). Unchanged.

Four rules that bind every memory write:

1. **Extract, don't dump.** No raw turns. Store distilled facts or summaries.
2. **Background, not hot-path.** Capture runs on session end / retirement (aux
   model), never inline per-turn.
3. **Never persist reasoning.** Every write path runs `extract_output()`;
   `<scratch_pad>` can never reach the store. Enforced by test.
4. **Gate by importance.** Trivia ("READY") never becomes a memory.

One store, one retrieval path (`KnowledgeMiddleware`) over **typed** entries
(`fact` / `conversation` / `ingest` / skill).

## 4. Consequences

- The Knowledge Store becomes high-signal — what the agent actually knows, not a
  transcript. Retrieval quality (and the `<learned_skills>` it injects) improves.
- No more reasoning leakage into the store or into downstream prompts.
- Less storage and fewer embeddings; cheaper retrieval.
- Phase 2 adds an aux-model extraction pass on session end — a modest background
  cost, the same cheap model `conversation_harvest` already uses.
- `<prior_sessions>` keeps a real role implementation surfaced: it gives
  *immediate* cross-session recency (written every terminal turn), which the
  checkpointer (same-thread only) and harvest (retirement-delayed) don't cover.
  So Phase 3 **cleans** it rather than dropping it — strip reasoning at the
  source + read, and collapse the two copy-pasted loaders into one.

## 5. Implementation (phased)

1. **Stop the bleeding** — delete the `add_finding(insight)` per-turn dump (keep
   session persistence). Enforce `extract_output()` on every write path +
   regression-test that scratch_pad can't reach the store. Sweep existing junk.
2. **Semantic extractor** — Mem0-style fact extraction on session end (background,
   aux model) + consolidate/dedupe + importance gating, `finding_type="fact"`.
3. **Rationalize overlap** — `<prior_sessions>` stays (it's the only *immediate*
   cross-session recency), but cleaned: strip reasoning at the persist source +
   at read (defensive for old files), and collapse the two duplicate loaders
   into one `load_prior_sessions`.
4. *(this ADR)* — the decision that never existed.

Phase 1 alone removes the junk and loses nothing real (the raw insights were
never useful); Phase 2 is the upgrade, not a prerequisite.

## 6. Alternatives considered

- **Just strip scratch_pad and keep the per-turn dump.** Rejected: even cleaned,
  storing every turn verbatim is the bloat anti-pattern and duplicates harvest.
- **Adopt a memory SDK (Mem0/LangMem) wholesale.** Deferred: protoAgent already
  has episodic (harvest) + procedural (skills) + an FTS5 store; we need the
  semantic-extraction piece, not a dependency. Revisit if requirements grow
  (graph memory, temporal validity).
