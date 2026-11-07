"""Semantic fact extraction for the session-end memory pass (ADR 0021).

The episodic side (``conversation_harvest``) summarizes a retired thread. This
is the **semantic** side: distil discrete, durable *facts* worth recalling in a
future, unrelated conversation — user preferences, decisions, stable facts about
their world/projects — and store them as core-memory rows (ADR 0125 P2-D3:
``kind='fact'``, ``provenance='extracted'``, ``review_state='pending'``). The
pre-0125 chunk backend (``finding_type="fact"`` chunks) is still handled for
non-harvest callers on a plugin store, but the retirement path no longer reaches
it — no code path writes memory through chunks (#4630).

Rules from the ADR:

- **Extract, don't dump.** The aux model returns short fact strings, not a
  transcript. Importance gating lives in the prompt — transient task state and
  pleasantries are dropped; a chatty turn with nothing durable yields ``[]``.
- **No leaked reasoning (ADR 0021 storage guardrail).** The chunk backend scrubbed
  leaked ``<scratch_pad>``/``<think>`` blocks inside ``add_chunk`` and refused
  content empty after stripping; ``MemoryStore.add`` inserts verbatim, so the
  extractor strips each fact itself (see ``_clean_fact``) and drops any that is
  pure reasoning — recallable core-memory rows never carry leaked reasoning.
- **Consolidate.** Before inserting, near-identical facts already in the store
  (scoped to the same ``namespace``) are skipped, so memory doesn't accrete
  duplicates.
- **Supersede, don't delete.** Core memories use MemoryStore's atomic subject,
  kind and scope funnel across provenances. Extractor date and operator guards
  retain refused revisions as superseded audit history. The legacy plugin chunk
  backend keeps its own invalidation protocol.
- **Conversation dates survive harvest.** The ``[as of YYYY-MM-DD]`` lead is
  excluded from similarity comparisons and passed to the funnel as valid_from.

Facts carry a ``namespace`` so per-project/owner scoping (ADR 0007) is a filter
later, not a migration.
"""

from __future__ import annotations

import json
import logging
import re

from langchain_core.messages import HumanMessage

from graph.output_format import strip_reasoning

log = logging.getLogger(__name__)

_MAX_FACTS = 12
_MAX_FACT_CHARS = 300
# ≥ this token-overlap (Jaccard) with an existing fact ⇒ treat as a duplicate and
# skip. Intentionally conservative (only near-identical facts are deduped).
_DEDUP_JACCARD = 0.85
# Token-overlap band [_SUPERSEDE_JACCARD, _DEDUP_JACCARD) ⇒ the new fact is a
# *revision* of the existing one (same subject, changed details): the old row is
# marked invalidated_at=now and the new row inserted (ADR 0069 D9 — supersede,
# don't delete). The comparison is purely deterministic — token sets plus the
# ``[as of]`` dates (an incoming fact dated BEFORE its match never supersedes it; an
# undated side falls back to "the incoming fact is newer") — never an LLM freshness
# call (Mem0's 2026 reversal + arXiv 2606.01435 are the ADR's basis for that rule).
_SUPERSEDE_JACCARD = 0.6
# Harvested facts carry an ``[as of YYYY-MM-DD]`` lead (the conversation's date, not the
# harvest's). Similarity is measured on the text AFTER it, see ``_split_as_of``.
_AS_OF_RE = re.compile(r"^\[as of (\d{4}-\d{2}-\d{2})\]\s*")

_FACTS_PROMPT = (
    "Extract durable, reusable FACTS from this conversation — things worth "
    "recalling in a future, unrelated conversation: the user's stable "
    "preferences, decisions made, and facts about their world, projects, or "
    "setup. Do NOT include pleasantries, transient task state, or one-off "
    "details. Each fact is one short, self-contained sentence.\n\n"
    "Output ONLY a JSON array of objects with subject and content strings. Use a "
    "short stable subject key (for example comp-floor) and put the fact in content. "
    "Reuse subject keys mentioned in the conversation. If nothing durable was shared, output "
    "[].\n\nConversation:\n{transcript}\n\nFacts (JSON array):"
)


def _parse_facts(raw: str) -> list[str | dict[str, str]]:
    """Pull structured facts or legacy fact strings out of a model response.

    The aux model may wrap the array in prose or a ```json fence; we grab the
    first bracketed array and parse it. Malformed / empty items are dropped,
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
    facts: list[str | dict[str, str]] = []
    for it in items:
        if isinstance(it, str) and it.strip():
            facts.append(it.strip()[:_MAX_FACT_CHARS])
        elif isinstance(it, dict) and isinstance(it.get("content"), str) and it["content"].strip():
            subject = it.get("subject")
            if isinstance(subject, str) and subject.strip():
                facts.append(
                    {"subject": subject.strip()[:_MAX_FACT_CHARS], "content": it["content"].strip()[:_MAX_FACT_CHARS]}
                )
        if len(facts) >= _MAX_FACTS:
            break
    return facts


async def _default_extractor(transcript: str, config) -> list[str | dict[str, str]]:
    """Aux-model fact extraction (classification-grade, not the main model)."""
    from graph.agent import _resolve_aux_model
    from graph.llm import create_llm

    llm = create_llm(config, model_name=_resolve_aux_model(config, ""))
    resp = await llm.ainvoke([HumanMessage(content=_FACTS_PROMPT.format(transcript=transcript))])
    return _parse_facts(str(resp.content))


def _clean_fact(fact: str) -> str:
    """ADR 0021 storage guardrail: scrub leaked ``<scratch_pad>``/``<think>`` reasoning from a
    fact before it persists. The chunk path got this for free from ``KnowledgeStore.add_chunk``
    (which stripped and then refused content empty after stripping); ``MemoryStore.add`` inserts
    content verbatim, so the extractor applies the same guard here. Returns the trimmed text —
    empty means the fact was pure leaked reasoning and must not be stored."""
    return strip_reasoning(fact or "").strip()


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[\w']+", text.lower()) if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def consolidate_and_store(
    store,
    facts: list[str | dict[str, str]],
    *,
    namespace: str | None = None,
    source: str | None = None,
) -> dict:
    """Consolidate extracted ``facts`` into ``store``, deduping near-duplicates and
    SUPERSEDING revisions (ADR 0069 D9). Returns counts (``added`` / ``skipped`` /
    ``superseded``). Best-effort: never raises.

    ADR 0125 P2-D3: the session-end extractor now writes to the core ``MemoryStore``
    (``kind='fact'``, ``provenance='extracted'``, ``pending``) — see
    :func:`_consolidate_into_memories`. A store exposing ``add_chunk`` is the pre-0125
    knowledge-chunk backend (plugin hosts, the forget/layered tests) and still gets the
    legacy chunk path below; nothing in the harvest path reaches it anymore.
    """
    if hasattr(store, "add_chunk"):
        return _consolidate_into_chunks(
            store,
            [f["content"] if isinstance(f, dict) else f for f in facts],
            namespace=namespace,
            source=source,
        )
    return _consolidate_into_memories(store, facts, namespace=namespace, source=source)


def _consolidate_into_memories(
    memory_store,
    facts: list[str | dict[str, str]],
    *,
    namespace: str | None = None,
    source: str | None = None,
) -> dict:
    """Resolve subjects and dedupe, then let the store own atomic supersession.

    String extractors retain similarity matching and date refreshes. Structured facts
    carry an explicit subject so even very different details share the same funnel.
    Reads are refreshed within the batch; failures remain best-effort for retirement.
    """
    counts = {"added": 0, "skipped": 0, "superseded": 0}
    src = source or "harvest"
    for raw_fact in facts:
        explicit = isinstance(raw_fact, dict)
        fact = _clean_fact(raw_fact["content"] if explicit else raw_fact)
        if not fact:
            continue
        new_as_of, body = _split_as_of(fact)
        subject = raw_fact.get("subject") if explicit else None
        try:
            existing = memory_store.list(kind="fact", scope=namespace or None, limit=500)
            existing = [row for row in existing if (row.scope or None) == (namespace or None)]
        except Exception:  # noqa: BLE001 — minimal stub or read failure ⇒ add-only
            existing = []
        # Deduplication also covers legacy NULL subjects and differently named keys.
        scored = [(_jaccard(_tokens(body), _tokens(_split_as_of(row.content)[1])), row) for row in existing]
        best, match = max(scored, key=lambda pair: pair[0], default=(0.0, None))
        if match is not None and best >= _DEDUP_JACCARD:
            old_as_of, _ = _split_as_of(match.content)
            # Keep the legacy date-refresh behaviour for extracted string facts.
            if explicit or match.provenance != "extracted" or not _refreshes(new_as_of, old_as_of):
                counts["skipped"] += 1
                continue
        matched_extracted_id = None
        if match is not None and best >= _SUPERSEDE_JACCARD and match.provenance == "extracted":
            if not explicit or not match.subject:
                matched_extracted_id = match.id
        if not subject:
            subject = match.subject if match is not None and best >= _SUPERSEDE_JACCARD else None
            subject = subject or body
        try:
            metadata = dict(
                kind="fact", provenance="extracted", review_state="pending",
                scope=namespace, source_session=src, thread_id=src,
            )
            funnel = getattr(memory_store, "add_with_supersession", None)
            if callable(funnel):
                rid, replaced = funnel(
                    fact, subject=subject, as_of=new_as_of,
                    protect_confirmed_operator=True, matched_extracted_id=matched_extracted_id, **metadata,
                )
            else:
                # Minimal non-core stores historically degrade to add-only. Never
                # emulate the funnel with a separate, non-atomic supersession pass.
                rid, replaced = memory_store.add(fact, **metadata), []
        except Exception:  # noqa: BLE001 — capture must not abort retirement
            log.warning("[memory] fact insert failed; prior facts left untouched", exc_info=True)
            continue
        if rid is not None:
            counts["added"] += 1
            counts["superseded"] += len(replaced)
    return counts


def _consolidate_into_chunks(
    knowledge_store,
    facts: list[str],
    *,
    namespace: str | None = None,
    source: str | None = None,
) -> dict:
    """Legacy pre-0125 chunk-backend consolidation (``domain="fact"`` chunks).

    Store ``facts`` as ``finding_type="fact"``, skipping near-duplicates of
    facts already present in the same ``namespace`` and SUPERSEDING revised
    ones (ADR 0069 D9). Returns counts
    (``added`` / ``skipped`` / ``superseded``).

    A new fact whose token overlap with an existing valid fact lands in the
    supersede band (``_SUPERSEDE_JACCARD`` ≤ Jaccard < ``_DEDUP_JACCARD``)
    replaces it: the new row is inserted FIRST, then the old row gets
    ``invalidated_at=now`` + ``invalidation_reason="superseded_by:<new id>"``
    (ADR 0108 D7.3; kept for audit — never UPDATE-in-place, never DELETE). A
    failed insert therefore never invalidates anything. The incoming fact wins
    unless its ``[as of]`` date is OLDER than the matched fact's: then it is the
    past state of something already stored, and is skipped (see
    ``_predates``). Undated facts keep "the incoming one is newer".
    Deterministic dates/ids, no LLM freshness judging. ``list_chunks`` excludes
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
        # the exact row; id None means "nothing to invalidate" (batch-local
        # entries, and COMMONS rows on a layered store: ids are per-backend,
        # so the commons id would name an unrelated PRIVATE row).
        candidates: list[tuple[int | None, set[str], str | None]] = [_candidate(c) for c in existing]
    except Exception:  # noqa: BLE001 — minimal stub or read failure ⇒ add-only
        candidates = []

    invalidate = getattr(knowledge_store, "invalidate_chunk", None)
    for fact in facts:
        new_as_of, body = _split_as_of(fact)
        ft = _tokens(body)
        scored = [(_jaccard(ft, toks), i) for i, (_, toks, _) in enumerate(candidates)]
        best, best_idx = max(scored, default=(0.0, -1))
        if best >= _DEDUP_JACCARD:
            cand_id, _, cand_as_of = candidates[best_idx]
            # Same fact. Keep the stored row unless this one carries a NEWER date (or the
            # stored one has none): then fall through and supersede it, so the fact's date
            # stays current. A commons row (id None) can't be invalidated here, so it dedups.
            if cand_id is None or not _refreshes(new_as_of, cand_as_of):
                counts["skipped"] += 1
                continue
        elif best >= _SUPERSEDE_JACCARD and _predates(new_as_of, candidates[best_idx][2]):
            # A revision dated BEFORE the fact it revises: harvest order isn't chronological,
            # so this is the past state of something we already hold a newer state for. It is
            # skipped, the same as an older near-duplicate above. It is NOT stored as
            # already-invalidated history: that is insert-then-invalidate, and if the
            # invalidation failed the stale fact would be left valid, which is this very bug.
            # What was true then stays in that thread's episodic summary.
            counts["skipped"] += 1
            continue
        # Revision of an existing fact (supersede band): remember the single best
        # match; it is invalidated only AFTER the new row has landed, so a failed
        # insert never loses the old fact (ADR 0108 D7.3).
        old_id = candidates[best_idx][0] if best >= _SUPERSEDE_JACCARD else None
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
        if rid is None:
            continue
        counts["added"] += 1
        if old_id is not None and callable(invalidate):
            if _supersede(invalidate, old_id, rid):
                counts["superseded"] += 1
                del candidates[best_idx]  # no longer valid — drop from comparisons
            else:
                log.warning(
                    "[memory] fact %s landed but its predecessor %s could not be invalidated — "
                    "both stay valid until the next revision",
                    rid,
                    old_id,
                )
        candidates.append((rid, ft, new_as_of))  # dedup/supersede within this batch too
    return counts


def _split_as_of(text: str) -> tuple[str | None, str]:
    """``("2026-08-11", body)`` for a fact stamped ``[as of …]``, else ``(None, text)``.

    Similarity is measured on the body. The date lead adds about five tokens, which
    would push an undated legacy row and its dated twin apart: 0.44 for a four-word
    fact, below the supersede band, so the same fact would be stored twice."""
    m = _AS_OF_RE.match(text or "")
    return (m.group(1), text[m.end() :]) if m else (None, text or "")


def _refreshes(new_as_of: str | None, old_as_of: str | None) -> bool:
    """Does a near-identical incoming fact carry a newer date than the stored one?"""
    return bool(new_as_of) and (old_as_of is None or new_as_of > old_as_of)


def _predates(new_as_of: str | None, old_as_of: str | None) -> bool:
    """Is a revision dated strictly BEFORE the stored fact it matches? Only when both carry
    a date: an undated side keeps the old "incoming is newer" rule, and the same date is
    not older."""
    return bool(new_as_of and old_as_of) and new_as_of < old_as_of


def _candidate(row) -> tuple[int | None, set[str], str | None]:
    """``(id-to-invalidate, token set, as-of date)`` for one existing fact. Rows are ``Chunk``
    objects on a plain store and — on a layered store — tier-tagged rows from BOTH
    tiers (``Chunk`` since #3245, dicts before it); a commons row keeps its content
    for dedup but gets id None so it is never invalidated through the private
    store."""
    get = row.get if isinstance(row, dict) else (lambda key, default=None: getattr(row, key, default))
    row_id = None if get("tier") == "commons" else get("id")
    as_of, body = _split_as_of(get("content") or "")
    return (row_id, _tokens(body), as_of)


def _supersede(invalidate, old_id: int, new_id: int) -> bool:
    """Invalidate ``old_id`` naming ``new_id`` as its replacement. A backend whose
    ``invalidate_chunk`` predates the ``superseded_by`` kwarg (an ADR 0031 plugin
    store) gets the legacy call — the row is still superseded, just unchained."""
    try:
        return bool(invalidate(old_id, superseded_by=new_id))
    except TypeError:
        return bool(invalidate(old_id))


async def extract_and_store_facts(
    transcript: str,
    *,
    memory_store,
    config,
    namespace: str | None = None,
    source: str | None = None,
    extractor=_default_extractor,
    as_of: str | None = None,
) -> dict:
    """Extract durable facts from ``transcript`` and consolidate them into the core
    ``MemoryStore`` (ADR 0125 P2-D3). Never raises — fact capture is best-effort and
    must not block thread retirement.

    ``as_of`` (``YYYY-MM-DD``) is when the conversation happened. Each fact is
    prefixed ``[as of …]`` so one harvested from an old thread reads as what was
    true THEN, not as the current state."""
    if memory_store is None or not transcript.strip():
        return {"added": 0, "skipped": 0, "superseded": 0}
    try:
        facts = await extractor(transcript, config)
    except Exception:  # noqa: BLE001
        log.exception("[memory] fact extraction failed")
        return {"added": 0, "skipped": 0, "superseded": 0}
    if as_of:
        facts = [
            {**f, "content": f"[as of {as_of}] {f['content']}"} if isinstance(f, dict) else f"[as of {as_of}] {f}"
            for f in facts
        ]
    try:
        counts = consolidate_and_store(memory_store, facts, namespace=namespace, source=source)
    except Exception:  # noqa: BLE001 — consolidation is designed never to raise, but a store
        # failure here must never propagate into harvest_thread (it re-raises under
        # raise_on_error AFTER the history summary is written). Fact capture is best-effort.
        log.exception("[memory] fact consolidation failed")
        return {"added": 0, "skipped": 0, "superseded": 0}
    if counts["added"] or counts["skipped"]:
        log.info(
            "[memory] facts: +%d new, %d dup-skipped, %d superseded (ns=%s)",
            counts["added"],
            counts["skipped"],
            counts["superseded"],
            namespace or "-",
        )
    return counts
