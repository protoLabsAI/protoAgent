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
  duplicates. (Superseding an *outdated* fact with a newer one is an LLM-judged
  refinement left for a follow-up; v1 dedups conservatively.)

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
_DEDUP_JACCARD = 0.85  # ≥ this token overlap with an existing fact ⇒ duplicate

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


def consolidate_and_store(knowledge_store, facts: list[str], *, namespace: str | None = None) -> dict:
    """Store ``facts`` as ``finding_type="fact"``, skipping near-duplicates of
    facts already present in the same ``namespace``. Returns counts.

    Best-effort: a store that lacks ``list_chunks`` (e.g. a minimal test stub)
    degrades to add-only. Never raises.
    """
    counts = {"added": 0, "skipped": 0}
    if not facts:
        return counts
    try:
        existing = knowledge_store.list_chunks(domain="fact", namespace=namespace, limit=500)
        existing_tokens = [_tokens(c.content) for c in existing]
    except Exception:  # noqa: BLE001 — minimal stub or read failure ⇒ add-only
        existing_tokens = []

    for fact in facts:
        ft = _tokens(fact)
        if any(_jaccard(ft, et) >= _DEDUP_JACCARD for et in existing_tokens):
            counts["skipped"] += 1
            continue
        # Facts live in their own domain (not "finding") so retrieval + the Store
        # view can distinguish semantic facts from other chunk types.
        rid = knowledge_store.add_chunk(
            fact,
            domain="fact",
            source="harvest",
            source_type="extracted",
            finding_type="fact",
            namespace=namespace,
        )
        if rid is not None:
            counts["added"] += 1
            existing_tokens.append(ft)  # dedup within this batch too
    return counts


async def extract_and_store_facts(
    transcript: str,
    *,
    knowledge_store,
    config,
    namespace: str | None = None,
    extractor=_default_extractor,
) -> dict:
    """Extract durable facts from ``transcript`` and consolidate them into the
    store. Never raises — fact capture is best-effort and must not block thread
    retirement."""
    if knowledge_store is None or not transcript.strip():
        return {"added": 0, "skipped": 0}
    try:
        facts = await extractor(transcript, config)
    except Exception:  # noqa: BLE001
        log.exception("[memory] fact extraction failed")
        return {"added": 0, "skipped": 0}
    counts = consolidate_and_store(knowledge_store, facts, namespace=namespace)
    if counts["added"] or counts["skipped"]:
        log.info("[memory] facts: +%d new, %d dup-skipped (ns=%s)",
                 counts["added"], counts["skipped"], namespace or "-")
    return counts
