"""Semantic fact extraction for the session-end memory pass (ADR 0021).

The episodic side (``conversation_harvest``) summarizes a retired thread. This
is the **semantic** side: distil discrete, durable *facts* worth recalling in a
future, unrelated conversation — user preferences, decisions, stable facts about
their world/projects — and store them as ``finding_type="fact"``.

Two rules from the ADR:

- **Extract, don't dump.** The aux model returns short fact strings, not a
  transcript. Importance gating lives in the prompt — transient task state and
  pleasantries are dropped; a chatty turn with nothing durable yields ``[]``.
- **Consolidate.** Before inserting, near-identical facts already in the store
  (scoped to the same ``namespace``) are skipped, so memory doesn't accrete
  duplicates.
- **Supersede, don't delete** (ADR 0069 D9). A new fact that *revises* an
  existing one — same subject, changed details, detected by a deterministic
  token-overlap band, never an LLM freshness judgment (Mem0's 2026 reversal +
  arXiv 2606.01435) — marks the old row ``invalidated_at=now`` and inserts the
  new row. History is kept for audit; retrieval excludes invalidated rows by
  default. Nothing here UPDATEs content in place or DELETEs.

Facts carry a ``namespace`` so per-project/owner scoping (ADR 0007) is a filter
later, not a migration.
"""

from __future__ import annotations

import json
import logging
import re

from langchain_core.messages import HumanMessage

log = logging.getLogger(__name__)

_MAX_FACTS = 12
_MAX_FACT_CHARS = 300
# ≥ this token-overlap (Jaccard) with an existing fact ⇒ treat as a duplicate and
# skip. Intentionally conservative (only near-identical facts are deduped).
_DEDUP_JACCARD = 0.85
# Token-overlap band [_SUPERSEDE_JACCARD, _DEDUP_JACCARD) ⇒ the new fact is a
# *revision* of the existing one (same subject, changed details): the old row is
# marked invalidated_at=now and the new row inserted (ADR 0069 D9 — supersede,
# don't delete). The comparison is purely deterministic — token sets plus
# "the incoming fact is newer by construction" — never an LLM freshness call
# (Mem0's 2026 reversal + arXiv 2606.01435 are the ADR's basis for that rule).
_SUPERSEDE_JACCARD = 0.6

_FACTS_PROMPT = (
    "Extract durable, reusable FACTS from this conversation — things worth "
    "recalling in a future, unrelated conversation: the user's stable "
    "preferences, decisions made, and facts about their world, projects, or "
    "setup. Do NOT include pleasantries, transient task state, or one-off "
    "details. Each fact is one short, self-contained sentence.\n\n"
    "Output ONLY a JSON array of strings. If nothing durable was shared, output "
    "[].\n\nConversation:\n{transcript}\n\nFacts (JSON array):"
)


def _parse_facts(raw: str) -> list[str]:
    """Pull a JSON array of fact strings out of a model response, defensively.

    The aux model may wrap the array in prose or a ```json fence; we grab the
    first bracketed array and parse it. Non-string / empty items are dropped,
    each fact is length-capped, and the list is capped at ``_MAX_FACTS``.
    """
    if not raw or not raw.strip():
        return []
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    facts: list[str] = []
    for it in items:
        if isinstance(it, str) and it.strip():
            facts.append(it.strip()[:_MAX_FACT_CHARS])
        if len(facts) >= _MAX_FACTS:
            break
    return facts


async def _default_extractor(transcript: str, config) -> list[str]:
    """Aux-model fact extraction (classification-grade, not the main model)."""
    from graph.agent import _resolve_aux_model
    from graph.llm import create_llm

    llm = create_llm(config, model_name=_resolve_aux_model(config, ""))
    resp = await llm.ainvoke([HumanMessage(content=_FACTS_PROMPT.format(transcript=transcript))])
    return _parse_facts(str(resp.content))


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[\w']+", text.lower()) if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def consolidate_and_store(
    knowledge_store,
    facts: list[str],
    *,
    namespace: str | None = None,
    source: str | None = None,
) -> dict:
    """Store ``facts`` as ``finding_type="fact"``, skipping near-duplicates of
    facts already present in the same ``namespace`` and SUPERSEDING revised
    ones (ADR 0069 D9). Returns counts
    (``added`` / ``skipped`` / ``superseded``).

    A new fact whose token overlap with an existing valid fact lands in the
    supersede band (``_SUPERSEDE_JACCARD`` ≤ Jaccard < ``_DEDUP_JACCARD``)
    replaces it: the old row gets ``invalidated_at=now`` (kept for audit —
    never UPDATE-in-place, never DELETE) and the new row is inserted. The
    incoming fact wins purely because it is newer — deterministic
    timestamps/ids, no LLM freshness judging. ``list_chunks`` excludes
    invalidated rows by default, so comparisons only ever run against
    currently-valid facts.

    ``source`` is the originating session/thread id (provenance, ADR 0069 D5);
    when the caller has none it falls back to the legacy ``"harvest"`` literal
    rather than an empty source.

    Best-effort: a store that lacks ``list_chunks`` (e.g. a minimal test stub)
    degrades to add-only, and one without ``invalidate_chunk`` skips the
    invalidation half of a supersede. Never raises.
    """
    counts = {"added": 0, "skipped": 0, "superseded": 0}
    if not facts:
        return counts
    try:
        existing = knowledge_store.list_chunks(domain="fact", namespace=namespace, limit=500)
        # (chunk id, token set) per valid fact — ids so a supersede can target
        # the exact row; id None marks batch-local entries (nothing to invalidate).
        candidates: list[tuple[int | None, set[str]]] = [(c.id, _tokens(c.content)) for c in existing]
    except Exception:  # noqa: BLE001 — minimal stub or read failure ⇒ add-only
        candidates = []

    invalidate = getattr(knowledge_store, "invalidate_chunk", None)
    for fact in facts:
        ft = _tokens(fact)
        scored = [(_jaccard(ft, toks), i) for i, (_, toks) in enumerate(candidates)]
        best, best_idx = max(scored, default=(0.0, -1))
        if best >= _DEDUP_JACCARD:
            counts["skipped"] += 1
            continue
        if best >= _SUPERSEDE_JACCARD:
            # Revision of an existing fact: invalidate the single best match,
            # then insert the new row below (supersede, don't delete).
            old_id = candidates[best_idx][0]
            if old_id is not None and callable(invalidate) and invalidate(old_id):
                counts["superseded"] += 1
                del candidates[best_idx]  # no longer valid — drop from comparisons
        # Facts live in their own domain (not "finding") so retrieval + the Store
        # view can distinguish semantic facts from other chunk types.
        rid = knowledge_store.add_chunk(
            fact,
            domain="fact",
            source=source or "harvest",
            source_type="extracted",
            finding_type="fact",
            namespace=namespace,
        )
        if rid is not None:
            counts["added"] += 1
            candidates.append((rid, ft))  # dedup/supersede within this batch too
    return counts


async def extract_and_store_facts(
    transcript: str,
    *,
    knowledge_store,
    config,
    namespace: str | None = None,
    source: str | None = None,
    extractor=_default_extractor,
) -> dict:
    """Extract durable facts from ``transcript`` and consolidate them into the
    store. Never raises — fact capture is best-effort and must not block thread
    retirement."""
    if knowledge_store is None or not transcript.strip():
        return {"added": 0, "skipped": 0, "superseded": 0}
    try:
        facts = await extractor(transcript, config)
    except Exception:  # noqa: BLE001
        log.exception("[memory] fact extraction failed")
        return {"added": 0, "skipped": 0, "superseded": 0}
    counts = consolidate_and_store(knowledge_store, facts, namespace=namespace, source=source)
    if counts["added"] or counts["skipped"]:
        log.info(
            "[memory] facts: +%d new, %d dup-skipped, %d superseded (ns=%s)",
            counts["added"],
            counts["skipped"],
            counts["superseded"],
            namespace or "-",
        )
    return counts
