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

log = logging.getLogger(__name__)

# RRF constant for the SECOND-level fusion ACROSS tiers (each tier already fused its own
# FTS5 ∪ vector). 60 is the standard default (matches HybridKnowledgeStore's rrf_k).
_RRF_K = 60


def _dedup_key(row: dict) -> str:
    """Identity for cross-tier de-dup: a chunk promoted into the commons has the SAME
    content as its private original, so key on content (ids differ across tiers)."""
    return (row.get("content") or "").strip()


class LayeredKnowledgeStore:
    """A knowledge store whose reads union a private + a commons backend, whose writes
    target private, and which can ``promote`` a private chunk into the commons."""

    def __init__(self, private, commons) -> None:
        self._private = private
        self._commons = commons

    def __getattr__(self, name):
        # Everything not overridden below (add_chunk/add_finding/add_document, the
        # delete_* family, get_hot_memory, stats, find_chunk_containing, reset_embed_breaker,
        # path, close, …) targets the PRIVATE store — writes never touch the commons.
        return getattr(self._private, name)

    # ── read: commons ∪ private, fused with RRF over rank ─────────────────────
    def search(self, query: str, k: int = 5, *, domain: str | None = None) -> list[dict]:
        """Top-k across BOTH tiers, fused by RRF over each tier's rank, tier-tagged.
        A chunk promoted into the commons (same content as its private original) is
        de-duped — the private record wins (it's editable) but keeps the summed score."""
        priv = self._private.search(query, k, domain=domain)
        comm = self._commons.search(query, k, domain=domain)

        fused: dict[str, dict] = {}
        scores: dict[str, float] = {}
        for tier, rows in (("commons", comm), ("private", priv)):  # private listed last → wins ties
            for rank, r in enumerate(rows):
                key = _dedup_key(r)
                if not key:
                    continue
                scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
                # First write seeds the record; private overwrites the tier tag + fields.
                if key not in fused or tier == "private":
                    fused[key] = {**r, "tier": tier}
        ranked = sorted(fused.values(), key=lambda r: scores[_dedup_key(r)], reverse=True)
        return ranked[:k]

    def list_chunks(self, *args, **kwargs) -> list[dict]:
        """Union both tiers' chunks, tier-tagged (backs the console's tier badges).
        Private first. Each chunk carries its own tier's row id (ids are per-backend)."""
        rows = [{**c.as_dict(), "tier": "private"} for c in self._private.list_chunks(*args, **kwargs)]
        rows += [{**c.as_dict(), "tier": "commons"} for c in self._commons.list_chunks(*args, **kwargs)]
        return rows

    def stats(self) -> dict:
        """Per-tier counts so callers can see the split (``private``/``commons``/``total``)."""
        priv = self._private.stats()
        comm = self._commons.stats()
        return {
            "private": int(priv.get("total", 0)),
            "commons": int(comm.get("total", 0)),
            "total": int(priv.get("total", 0)) + int(comm.get("total", 0)),
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
