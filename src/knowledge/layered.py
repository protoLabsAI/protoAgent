"""Layered knowledge (ADR 0041 slice 3 / bd-2wu) — read COMMONS ∪ PRIVATE, write private.

The knowledge analog of :mod:`graph.skills.layered`. An agent reads both the shared
**commons** knowledge library (host-level, read by every agent on the box) and its own
**private** store, but **writes go to private** — so an agent's in-progress facts never
pollute the fleet. Sharing is **promotion-defined**: an operator explicitly promotes
a proven private chunk into the commons (curated, never automatic — ADR 0041). It's the
"shared brain, private hands" model, same as skills.

Search **fuses both tiers** with a second-level RRF over rank (each tier already did its
own FTS5 ∪ vector RRF internally). All other methods — writes, hot memory, deletes, stats,
ingestion — **delegate to private** via ``__getattr__``; only ``search``/``list_chunks``
(which union tiers) and ``promote``/``forget_from_commons`` (commons curation) are
overridden. Drop-in for ``KnowledgeStore`` everywhere the runtime uses it.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import fields, is_dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowledge.store import Chunk

from knowledge.store import REVIEW_CONFIRMED

log = logging.getLogger(__name__)

# RRF constant for the SECOND-level fusion ACROSS tiers (each tier already fused its own
# FTS5 ∪ vector). 60 is the standard default (matches HybridKnowledgeStore's rrf_k).
_RRF_K = 60

# ``stats()`` keys that are NOT domains: the grand total and the tier split. Kept in
# step with ``graph.snapshot_op._NON_DOMAIN_STAT_KEYS`` (the seed's domain discovery).
_SPLIT_KEYS = frozenset({"total", "private", "commons"})


def _dedup_key(row: dict) -> tuple | None:
    """Cross-tier equivalence using the provenance preserved by promotion.

    There is no persisted promotion lineage. Text alone cannot establish that two
    sources are the same. Review state is intentionally excluded: promotion confirms
    the commons copy while leaving the private verdict unchanged.
    """
    content = (row.get("content") or "").strip()
    if not content:
        return None
    return (content, *(row.get(field) or "" for field in (
        "domain", "namespace", "source", "source_type", "heading", "finding_type",
        "epoch", "memory_kind", "subject", "expires_at", "delivery_policy",
    )))


def _tag(chunk, tier: str):
    """Stamp a backend's ``list_chunks`` row with its tier, keeping the row's TYPE.

    Real backends yield ``Chunk`` dataclasses (``knowledge.store.Chunk`` has a ``tier``
    field for exactly this); a custom ``KnowledgeBackend`` may yield dicts, which get
    a ``"tier"`` key instead. Either way the caller sees the same shape it would from
    the backend itself — the layered store is a drop-in, not a different API.

    A dataclass WITHOUT a ``tier`` field (a backend's own row type — unreachable today,
    but the Protocol allows it) degrades to its ``as_dict()`` plus the key rather than
    a ``TypeError`` from ``replace``.
    """
    if is_dataclass(chunk) and not isinstance(chunk, type) and "tier" in {f.name for f in fields(chunk)}:
        return replace(chunk, tier=tier)
    if hasattr(chunk, "as_dict"):
        return {**chunk.as_dict(), "tier": tier}
    return {**dict(chunk), "tier": tier}


class LayeredKnowledgeStore:
    """A knowledge store whose reads union a private + a commons backend, whose writes
    target private, and which can ``promote`` a private chunk into the commons."""

    def __init__(self, private, commons) -> None:
        self._private = private
        self._commons = commons

    def __getattr__(self, name):
        # Everything not overridden below (add_chunk/add_finding/add_document, the
        # delete_*/purge_domain family, invalidate_chunk/set_review_state (ADR 0108 D7),
        # get_hot_memory, stats, find_chunk_containing, reset_embed_breaker, path,
        # close, …) targets the PRIVATE store — writes (and purges, and review
        # verdicts) never touch the commons; it's curated via promote/forget only.
        return getattr(self._private, name)

    # ── read: commons ∪ private, fused with RRF over rank ─────────────────────
    def search(
        self,
        query: str,
        k: int = 5,
        *,
        domain: str | None = None,
        namespace: str | list[str] | None = None,
        include_invalidated: bool = False,
        epoch: str | None = None,
        memory_kind: str | None = None,
        review_state: str | None = None,
        delivery_policy: str | None = None,
        deliverable: bool = False,
        prefix: bool = False,
    ) -> list[dict]:
        """Top-k across BOTH tiers, fused by RRF over each tier's rank, tier-tagged.
        Cross-tier copies with matching text and provenance are paired one-to-one;
        the private record wins (it's editable) and keeps the summed score. Distinct
        records within a tier are never collapsed, even when their text is identical.
        ``namespace`` (ADR 0069 D3a), ``include_invalidated`` (ADR 0069 D9 —
        superseded rows are excluded by default), ``epoch`` (#1634 — era scoping),
        ``memory_kind`` / ``review_state`` (#3072 — typed-memory classification),
        ``delivery_policy`` (ADR 0108 D4) and ``deliverable`` (ADR 0108 D6 — the
        delivery layer's rejected/expired exclusion) and ``prefix`` (#3293 — the type-ahead's
        trailing partial word) are passed through to both tiers."""
        priv = self._private.search(
            query, k, domain=domain, namespace=namespace, include_invalidated=include_invalidated, epoch=epoch,
            memory_kind=memory_kind, review_state=review_state, delivery_policy=delivery_policy,
            deliverable=deliverable, prefix=prefix,
        )
        comm = self._commons.search(
            query, k, domain=domain, namespace=namespace, include_invalidated=include_invalidated, epoch=epoch,
            memory_kind=memory_kind, review_state=review_state, delivery_policy=delivery_policy,
            deliverable=deliverable, prefix=prefix,
        )

        fused: list[dict] = []
        scores: list[float] = []
        commons_matches: dict[tuple, deque[int]] = defaultdict(deque)
        for tier, rows in (("commons", comm), ("private", priv)):
            for rank, row in enumerate(rows):
                key = _dedup_key(row)
                if key is None:
                    continue
                score = 1.0 / (_RRF_K + rank)
                matches = commons_matches.get(key)
                if tier == "private" and matches:
                    index = matches.popleft()
                    fused[index] = {**row, "tier": tier}
                    scores[index] += score
                else:
                    index = len(fused)
                    fused.append({**row, "tier": tier})
                    scores.append(score)
                    if tier == "commons":
                        commons_matches[key].append(index)
        ranked = sorted(range(len(fused)), key=scores.__getitem__, reverse=True)
        return [fused[index] for index in ranked[:k]]

    def list_chunks(self, *args, **kwargs) -> list[Chunk] | list[dict]:
        """Union both tiers' chunks, tier-tagged (backs the console's tier badges).
        Private first. Each chunk carries its own tier's row id (ids are per-backend).

        Rows keep the backend's own type — ``Chunk`` objects with ``.tier`` set (so
        ``as_dict()`` carries ``"tier"``), never a different shape. Every consumer of
        ``list_chunks`` reads rows by attribute (``memory_list``, the fact
        consolidator, the snapshot seed); returning dicts here made all three fail
        silently or loudly the moment a commons was configured."""
        rows = [_tag(c, "private") for c in self._private.list_chunks(*args, **kwargs)]
        rows += [_tag(c, "commons") for c in self._commons.list_chunks(*args, **kwargs)]
        return rows

    def stats(self) -> dict:
        """Per-domain chunk counts merged across BOTH tiers, plus the tier split.

        Shape: ``{<domain>: n, ..., "total": N, "private": P, "commons": C}`` — the same
        per-domain keys a single ``KnowledgeStore.stats()`` returns (summed over the
        tiers) so readers that treat every non-``total`` key as a domain
        (``memory_stats``, the snapshot seed's domain discovery, the Store view) see
        real domains, while readers of the split keys keep working. A domain literally
        named ``private``/``commons``/``total`` is shadowed by the split keys."""
        priv = self._private.stats() or {}
        comm = self._commons.stats() or {}
        merged: dict[str, int] = {}
        for tier_stats in (priv, comm):
            for domain, n in tier_stats.items():
                if domain in _SPLIT_KEYS:
                    continue
                merged[domain] = merged.get(domain, 0) + int(n)
        return {
            **merged,
            "total": int(priv.get("total", 0)) + int(comm.get("total", 0)),
            "private": int(priv.get("total", 0)),
            "commons": int(comm.get("total", 0)),
        }

    # ── commons curation: promote (private→commons) + forget ──────────────────
    def promote(self, chunk_id: int) -> dict | None:
        """Lift a PRIVATE chunk (by id) into the commons. **Idempotent**: a chunk whose
        content is already in the commons isn't duplicated. Returns the chunk dict, or
        None if no private chunk by that id exists / the commons write didn't land
        (e.g. an unwritable commons). Curated, explicit — the commons is trusted."""
        chunk = self._private.get_chunk(chunk_id)
        if chunk is None:
            return None
        content = chunk.get("content") or ""
        if self._commons.id_for_exact_content(content) is not None:
            return {**chunk, "tier": "commons"}  # already shared — no-op
        self._commons.add_chunk(
            content,
            domain=chunk.get("domain") or "general",
            heading=chunk.get("heading"),
            source=chunk.get("source"),
            source_type=chunk.get("source_type"),
            finding_type=chunk.get("finding_type"),
            namespace=chunk.get("namespace"),
            epoch=chunk.get("epoch"),
            memory_kind=chunk.get("memory_kind"),
            subject=chunk.get("subject"),
            # Promotion IS the operator's curation (ADR 0041) — the commons copy is
            # confirmed by construction, whatever the private row's verdict was,
            # and nothing reviews a commons row afterwards (ADR 0108 D7.2).
            review_state=REVIEW_CONFIRMED,
            expires_at=chunk.get("expires_at"),
            delivery_policy=chunk.get("delivery_policy"),
        )
        if self._commons.id_for_exact_content(content) is None:
            log.error("[knowledge] promote(%s): commons write did not land — is the commons writable?", chunk_id)
            return None
        log.info("[knowledge] promoted chunk %s into the commons", chunk_id)
        return {**chunk, "tier": "commons"}

    def forget_from_commons(self, chunk_id: int) -> bool:
        """Remove a chunk from the shared commons by its COMMONS id — the inverse of
        :meth:`promote`. Returns False when no commons chunk by that id exists. Never
        touches the private tier."""
        return bool(self._commons.delete_by_id(chunk_id))

    def close(self) -> None:
        for store in (self._private, self._commons):
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
