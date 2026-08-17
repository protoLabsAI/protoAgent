# 0101 — Context lifecycle: log, surface, pressure

Status: **Accepted** (operator-approved 2026-08-17; umbrella #2772)

## Context

No decision record governs how a session's context grows, shrinks, or gets priced.
Compaction is config (`graph/config.py:491-494`) plus a LangChain subclass
(`graph/middleware/compaction.py`); tool-result size limits are scattered module
constants; prompt caching is a middleware with no stated contract. A 2026-08-17
audit of the full context path (prompt assembly, history persistence, token
accounting) against the mechanisms behind DeepSeek Harness's context system —
event-sourced log with a derived model-visible surface, pressure-driven compaction
that prunes before it summarizes, and strict prefix-cache discipline — found the
following.

**What we already do right.** The stable prefix is byte-stable by deliberate
design: the system prompt is frozen at graph build (`graph/agent.py:1119-1146`),
contains no timestamps or session ids, and `PromptCacheMiddleware` places a
`cache_control` breakpoint on it (`graph/middleware/prompt_cache.py:95-116`, on by
default). The Claude Code identity block on the anthropic-oauth path composes
byte-exactly ahead of it (`graph/middleware/claude_code_identity.py:42-49`). Tool
ordering is deterministic within a build. Subagents structurally conserve parent
context by returning only their final message (`graph/agent.py:497-521`).

**Where the payoff is thrown away.** A real session measured a **30.9% cache hit
ratio** (cache-disciplined harnesses measure ~99.9%); one protoEngineer turn cost
**$58.83 across 38 LLM calls** (the 2026-08-14 context-cost audit; see #2772).
Three causes:

1. **No breakpoints on message history.** We use one of Anthropic's four slots.
   In a long agentic turn the growing history dominates input tokens and is
   re-sent uncached every round.
2. **The volatile context block poisons any fix.** `KnowledgeMiddleware` composes
   RAG hits, hot memory, `<working_state>`, and an MRU-reordered skills index
   *per model call* (`graph/middleware/knowledge.py:281-421`) and it lands as
   system block #2 — between the cached prefix and the messages. Anthropic's
   cache key is prefix-based; a churning mid-prefix block invalidates any history
   breakpoint every round.
3. **Subagents get no caching at all** — the subagent middleware stack omits
   `PromptCacheMiddleware` entirely (`graph/agent.py:400-441`).

**Size and overflow have no policy.** MCP tool outputs are completely uncapped
(`tools/mcp_tools.py:466-471` — #2345–#2350 added time/concurrency bounds, not
size). Every built-in cap applies at call time only; once a `ToolMessage` is in
the checkpointer it is re-sent verbatim until compaction removes the *entire*
history — an all-or-nothing lever. Nothing anywhere catches a context-length
provider error: `ModelFallbackMiddleware` re-sends the same oversized prompt to a
different model, and the next turn on the thread hits the same wall.

**Compaction destroys what it could fold.** Auto-compaction rewrites the
checkpoint in place; with `checkpoint_keep_per_thread=2` pruning the
summarized-away history is gone — no archive. The never-lossy manual `/compact`
(`graph/compaction_op.py` — archives the full transcript to the knowledge store,
refuses if it can't) sits behind a dev flag past its own `remove_by` date
(`runtime/flags.py:47-53`). "What did the model see on turn N" is unanswerable:
prompt snapshots record the system prompt only, never the message list.

## Decision

**D1 — Cache discipline is a contract, not an accident.** The request prefix
(tool schemas → system → history) must stay byte-stable across consecutive
requests on a thread, and the breakpoint budget is spent deliberately: one slot
on the stable system block (as today), up to two rolling slots on the message
history tail so round N+1 reads round N's history from cache. The subagent stack
gets `PromptCacheMiddleware` — its prompts are static per build. Known
invalidation vectors (model/effort switch, hot reload, tool-deferral widening)
are accepted as-is but must not multiply. (#2777, #2778)

**D2 — Volatile context rides the message stream, not the system prompt.** The
injected layer (RAG, hot memory, working state, skills index) is composed **once
per turn** and delivered in the turn's input frame; the system message becomes
`[identity][stable, cache_control]` and nothing between prefix and history
churns. Intra-turn variance is frozen the same way: skills MRU order and
`<working_state>` reads become per-turn, not per-call. ADR 0069's delivery
contract (attribution digest, untrusted-reference envelope, incognito scoping)
moves intact — position changes, contract doesn't. (#2776, #2779)

**D3 — Every tool result is bounded and prunable.** All tool outputs are
size-capped at call time — including MCP via a new `mcp.max_result_chars`
(default ~50k, matching `_MAX_READ_CHARS`) — using one convention: bounded head +
fixed omission marker + bounded tail, so both ends survive. Results already in
history become prunable: at a soft pressure threshold (~60% of window) tool
results older than N rounds are rewritten to head+tail stubs, batched and late so
the one-time cache invalidation is amortized, preserving AIMessage/ToolMessage
pairing per `compaction_op`'s `_safe_cut_index` discipline. (#2781, #2782)

**D4 — Relief has an order: prune, then summarize, then recover.** Pruning
(near-lossless) always runs before summarization (lossy) is considered. A
context-length provider error is caught once: force compaction, retry the call
once, count it; a second failure surfaces honestly, but the thread is smaller.
(#2782, #2783)

**D5 — Fold, never destroy.** Auto-compaction becomes archive-first, reusing
`/compact`'s existing machinery (knowledge store, `chat-archive:<session_id>`
namespace) before the rewrite. Failure mode, decided: attempt the archive; on
failure, compact anyway with a loud log line and a metric — safety-valve duty
outranks purity on the automatic path. The manual `/compact` keeps its strict
never-lossy refusal, and its dev flag (past `remove_by`) is removed. (#2784,
#2785)

**D6 — Pressure is measured, persisted, and projected.** `context_tokens` and
per-turn cache-hit % are persisted to `TelemetryStore` (guarded `ALTER`, the
#2701 pattern); the `context-v1` part gains a projected-next-request estimate
(chars//4 heuristic is fine — precision belongs to the adapter layer). The
console surfaces projection and a per-session series instead of the current
wrong-axis fallback. Extends the ADR 0006 pattern; no new store. (#2773, #2787)

**D7 — Cache TTL is tiered by agent profile.** Fleet/desktop agent profiles
default to the already-plumbed `ttl: "1h"` (`prompt_cache.py:89-93`); everything
else stays 5m. Revisit with D6 data. (#2780)

**D8 — Round count is governed independently.** The circuit breaker (#2710) is
adopted as part of this lifecycle: a soft checkpoint at N rounds injects a
steering nudge to re-ground on persona/plan (the 2026-08-15 duplicate-card
incident showed instruction adherence decays with round count), with a
configurable hard cap above it. D1 makes rounds cheap; D8 keeps them coherent.

**Deferred — the true log/surface split.** Derived-view compaction via
`ModelRequest.override(messages=...)` (the seam exists unused in LangChain 1.3.6;
`pre_model_hook`/`llm_input_messages` are gone in 1.x) would keep full history in
the checkpointer and shrink only the model-visible surface. Deferred by operator
decision to its own ADR after Phases 0–2 land and D6 telemetry can justify it —
it changes checkpoint-growth behavior and pruning strategy. (#2786)

## Rejected alternatives

- **Truncation instead of pruning/summarization** — destroys the tail or head
  wholesale; the head+marker+tail convention preserves the parts models actually
  key on.
- **Continuous pruning** — nibbling history every round invalidates the D1
  history breakpoints every round; batched-late amortizes to one miss per pass.
- **Refuse-on-archive-failure for auto-compaction** — correct for the manual
  path, wrong for the safety valve: refusing converts a recoverable pressure
  event into a hard turn failure.
- **Doing D1 without D2** — history breakpoints behind a per-call-churning
  system block never hit; measured, not hypothetical.
- **A real tokenizer for the meter** — the 4-chars heuristic plus
  provider-reported usage is within a few percent at the decision points that
  matter (trigger thresholds); a tokenizer dependency buys precision nothing
  here consumes.

## Consequences

Cache hit ≥80% on Anthropic paths becomes the acceptance bar for Phase 1
(#2777); long turns get cheaper without touching round count; overflow becomes a
recovered event; no history is lost to automatic compaction. The costs: one-time
cache misses at each prune/compaction pass, per-turn (not per-call) freshness for
injected memory and working state within a turn, and knowledge-store growth from
compaction archives (bounded by existing knowledge retention).
