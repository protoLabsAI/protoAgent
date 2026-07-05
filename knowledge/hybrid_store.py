"""Reference embeddings-on-FTS5 knowledge store.

The base ``KnowledgeStore`` (knowledge/store.py) is FTS5 + LIKE only, and its
docstring invites forks that want semantic search to *subclass and override
``search()``*. This is that reference implementation, backported from the
protoLabs fleet (protoResearcher / gina), generalised:

- **Pluggable embeddings.** Pass ``embed_fn(text) -> list[float]`` (wire it to
  Ollama, the LiteLLM gateway, sentence-transformers, …). With ``embed_fn=None``
  the store behaves exactly like the FTS5 base — so it's a safe drop-in.
- **Hybrid retrieval via RRF.** Fuses the base FTS5 ranking with a vector
  ranking using Reciprocal Rank Fusion, so lexical and semantic hits reinforce
  each other without tuning a weight.
- **Embedding circuit breaker.** If ``embed_fn`` errors repeatedly, the breaker
  opens for a cooldown and search silently falls back to FTS5 — an embedding
  outage degrades quality, never availability.

Storage: vectors live in a side ``chunk_vectors`` table in the same DB; query
time does brute-force cosine in Python. That's fine for a template's
operator-notes-scale store — forks at scale should swap in ``sqlite-vec`` or a
real vector DB (override ``_vector_search``).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections.abc import Callable

from knowledge.store import _BULK_DELETE_REASON, KnowledgeStore, _namespace_clause, _normalize_before

log = logging.getLogger(__name__)

EmbedFn = Callable[[str], "list[float]"]


class HybridKnowledgeStore(KnowledgeStore):
    """KnowledgeStore + optional semantic search (RRF over FTS5 ∪ vectors)."""

    def __init__(
        self,
        db_path=None,
        *,
        embed_fn: EmbedFn | None = None,
        embed_batch_fn: Callable[[list[str]], list[list[float]]] | None = None,
        vector_k: int = 20,
        rrf_k: int = 60,
        min_score: float = 0.0,
        breaker_threshold: int = 2,
        breaker_cooldown_s: float = 300.0,
        preview_chars: int = 1000,
        chunk_max_chars: int = 1200,
        chunk_overlap_chars: int = 150,
        chunk_min_chars: int = 200,
        context_fn: Callable[[str, str], str] | None = None,
        scoped: bool = True,
    ):
        super().__init__(
            db_path,
            preview_chars=preview_chars,
            chunk_max_chars=chunk_max_chars,
            chunk_overlap_chars=chunk_overlap_chars,
            chunk_min_chars=chunk_min_chars,
            context_fn=context_fn,
            scoped=scoped,
        )
        self._embed_fn = embed_fn
        # Optional batched embedder (texts -> vectors in one request). When set,
        # add_document embeds a whole document's chunks in a single round-trip
        # instead of N serial _embed calls.
        self._embed_batch_fn = embed_batch_fn
        self._vector_k = vector_k
        self._rrf_k = rrf_k
        # Relevance floor: drop fused hits whose RRF score is below this. 0 keeps
        # all (today's behavior). >0 stops off-topic turns from injecting weak,
        # best-effort chunks — tune against the retrieval eval, since RRF scores
        # aren't normalized across queries.
        self._min_score = max(0.0, float(min_score))
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown_s = breaker_cooldown_s
        self._embed_failures = 0
        self._breaker_open_until = 0.0
        if embed_fn is not None:
            self._ensure_vectors_table()

    # ── circuit breaker ────────────────────────────────────────────────────────

    def _breaker_open(self) -> bool:
        return time.monotonic() < self._breaker_open_until

    def _record_embed_failure(self) -> None:
        self._embed_failures += 1
        if self._embed_failures >= self._breaker_threshold:
            self._breaker_open_until = time.monotonic() + self._breaker_cooldown_s
            log.warning(
                "[knowledge] embedding circuit opened for %.0fs after %d failures",
                self._breaker_cooldown_s,
                self._embed_failures,
            )

    def _record_embed_success(self) -> None:
        self._embed_failures = 0
        self._breaker_open_until = 0.0

    def reset_embed_breaker(self) -> bool:
        """Force the embedding circuit closed immediately.

        Called when the gateway key is confirmed good out-of-band (a successful
        "Test connection" of the live key) so semantic recall resumes at once
        instead of waiting out ``breaker_cooldown_s``. Returns True if the
        breaker was actually open (something changed) — lets callers log only
        when it mattered. A no-op when already closed."""
        was_open = self._breaker_open() or self._embed_failures > 0
        self._record_embed_success()
        return was_open

    def _probe_once(self) -> bool:
        """One synchronous embedding-route probe: on failure, open the breaker
        IMMEDIATELY (not after ``breaker_threshold`` in-turn failures) so no chat
        turn pays for a dead route. Returns True when the route is healthy."""
        if self._embed_fn is None:
            return False
        try:
            self._embed_fn("ping")
            self._record_embed_success()
            return True
        except Exception as exc:  # noqa: BLE001 — a probe failure must only open the breaker
            log.warning(
                "[knowledge] embedding probe failed — semantic recall paused for %.0fs "
                "(keyword recall continues): %s",
                self._breaker_cooldown_s,
                exc,
            )
            self._embed_failures = self._breaker_threshold
            self._breaker_open_until = time.monotonic() + self._breaker_cooldown_s
            return False

    def warm_probe(self) -> None:
        """Fire-and-forget ``_probe_once`` on a daemon thread (#1681). Recall runs
        BEFORE every model call, so without this the FIRST turns of an embedding
        outage each ate the full transport timeout before the in-turn breaker could
        trip — the operator experienced a frozen chat. Probing at construction moves
        that cost off the user's turn entirely."""
        if self._embed_fn is None:
            return
        import threading

        threading.Thread(target=self._probe_once, daemon=True, name="knowledge-embed-probe").start()

    def _embed(self, text: str) -> list[float] | None:
        if self._embed_fn is None or self._breaker_open():
            return None
        try:
            vec = self._embed_fn(text)
            self._record_embed_success()
            return [float(x) for x in vec]
        except Exception as exc:  # noqa: BLE001 - breaker by design
            log.warning("[knowledge] embed_fn failed: %s", exc)
            self._record_embed_failure()
            return None

    def _embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Embed a list of texts in one call (shares the circuit breaker with
        ``_embed``). Returns None — caller degrades to FTS5 — when batching is
        unavailable, the breaker is open, or the request fails."""
        if self._embed_batch_fn is None or self._breaker_open():
            return None
        try:
            vecs = self._embed_batch_fn(texts)
            self._record_embed_success()
            return [[float(x) for x in v] for v in vecs]
        except Exception as exc:  # noqa: BLE001 - breaker by design
            log.warning("[knowledge] embed_batch_fn failed: %s", exc)
            self._record_embed_failure()
            return None

    # ── vector storage ──────────────────────────────────────────────────────────

    def _ensure_vectors_table(self) -> None:
        db = self._get_db()
        if db is None:
            return
        try:
            db.execute("CREATE TABLE IF NOT EXISTS chunk_vectors (chunk_id INTEGER PRIMARY KEY, vec TEXT NOT NULL)")
            db.commit()
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] could not create chunk_vectors: %s", exc)
        finally:
            db.close()

    def add_chunk(self, content: str, domain: str = "general", heading=None, **kw) -> int | None:
        chunk_id = super().add_chunk(content, domain, heading, **kw)
        if chunk_id is None or self._embed_fn is None:
            return chunk_id
        # Embed the heading+content so semantic search sees the same text FTS5 does.
        text = (heading + "\n" if heading else "") + content
        vec = self._embed(text)
        if vec is not None:
            db = self._get_db()
            if db is not None:
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO chunk_vectors (chunk_id, vec) VALUES (?, ?)",
                        (chunk_id, json.dumps(vec)),
                    )
                    db.commit()
                except sqlite3.DatabaseError as exc:
                    log.warning("[knowledge] store vector failed for %d: %s", chunk_id, exc)
                finally:
                    db.close()
        return chunk_id

    def add_document(self, content: str, domain: str = "general", heading=None, **kw) -> list[int]:
        """Chunk + enrich, then embed ALL of the document's chunks in ONE batched
        request instead of N serial ``_embed`` calls (ADR 0021).

        Falls back to the base per-chunk path (each piece embedded via
        ``add_chunk``) when there's nothing to batch — a single chunk, no batched
        embedder, embeddings off, or the breaker open. Rows are always written
        first, so an embed failure still leaves FTS5-searchable chunks."""
        # Pull the chunk-knob + enrich kwargs out for _chunk_and_enrich; the rest
        # (domain/heading/source/…) are chunk-write kwargs.
        prep_kw = {k: kw.pop(k) for k in ("max_chars", "overlap_chars", "min_chars", "enrich") if k in kw}
        texts = self._chunk_and_enrich(content, **prep_kw)
        batchable = (
            len(texts) > 1
            and self._embed_fn is not None
            and self._embed_batch_fn is not None
            and not self._breaker_open()
        )
        if not batchable:
            # Base path: per-chunk add_chunk (single embed each, or FTS-only).
            ids: list[int] = []
            for text in texts:
                cid = self.add_chunk(text, domain, heading, **kw)
                if cid is not None:
                    ids.append(cid)
            return ids

        # Batched path: write rows WITHOUT per-chunk embed (the BASE add_chunk),
        # then one embed call for the whole document, then bulk-store the vectors.
        rows: list[tuple[int, str]] = []
        for text in texts:
            cid = KnowledgeStore.add_chunk(self, text, domain, heading, **kw)
            if cid is not None:
                # Embed heading+content so vector search sees what FTS5 sees.
                rows.append((cid, (heading + "\n" if heading else "") + text))
        if not rows:
            return []
        vecs = self._embed_batch([t for _, t in rows])
        if vecs is not None and len(vecs) == len(rows):
            self._store_vectors([(cid, v) for (cid, _), v in zip(rows, vecs)])
        return [cid for cid, _ in rows]

    def _store_vectors(self, pairs: list[tuple[int, list[float]]]) -> None:
        """Bulk-insert chunk vectors in one transaction."""
        db = self._get_db()
        if db is None:
            return
        try:
            db.executemany(
                "INSERT OR REPLACE INTO chunk_vectors (chunk_id, vec) VALUES (?, ?)",
                [(cid, json.dumps(v)) for cid, v in pairs],
            )
            db.commit()
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] batch store vectors failed: %s", exc)
        finally:
            db.close()

    def delete_by_namespace(self, namespace: str) -> int:
        """Drop the namespace's chunks AND their vectors (no FK cascade on the
        side table) so ephemeral chunks leave nothing behind."""
        if not namespace:
            return 0
        db = self._get_db()
        if db is not None:
            try:
                db.execute(
                    "DELETE FROM chunk_vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE namespace = ?)",
                    (namespace,),
                )
                db.commit()
            except sqlite3.DatabaseError as exc:
                log.warning("[knowledge] delete_by_namespace vectors failed: %s", exc)
            finally:
                db.close()
        return super().delete_by_namespace(namespace)

    def purge_domain(self, domain: str, *, before=None) -> int:
        """Purge the domain's chunks AND their vectors (#1634) — the
        :meth:`delete_by_namespace` pattern: no FK cascade on the side table, so
        the vectors go first (under the SAME cutoff — the base delete's FTS
        trigger covers ``chunks_fts``, nothing covers ``chunk_vectors``). A
        purged chunk must vanish from BOTH search modes, not linger as a
        vector-only hit."""
        if not domain or not domain.strip():
            return 0
        try:
            cutoff = _normalize_before(before)
        except ValueError:
            # Mirror the base refusal BEFORE touching vectors — a bad cutoff must
            # not strip embeddings off rows that then survive the row delete.
            log.warning("[knowledge] purge_domain(%r): unparseable before=%r — refusing to purge", domain, before)
            return 0
        db = self._get_db()
        if db is not None:
            try:
                sql = "DELETE FROM chunk_vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE domain = ?"
                params: list = [domain]
                if cutoff is not None:
                    sql += " AND created_at < ?"
                    params.append(cutoff)
                db.execute(sql + ")", params)
                db.commit()
            except sqlite3.DatabaseError as exc:
                log.warning("[knowledge] purge_domain vectors failed: %s", exc)
            finally:
                db.close()
        return super().purge_domain(domain, before=cutoff)

    def purge_invalidated(self, older_than_seconds: int = 0, *, _cutoff: str | None = None) -> int:
        """Sweep past-grace bulk-soft-deleted chunks AND their vectors (#1770) — the
        :meth:`delete_by_namespace` / :meth:`purge_domain` pattern: no FK cascade
        on the side table, so the vectors go first under the SAME cutoff (shared
        via ``_cutoff`` so a swept chunk can't linger as a vector-only hit).

        Mirrors the base's ``invalidation_reason`` filter so only bulk delete-by-source
        vectors are dropped — auto-supersession audit rows (ADR 0069 D9) keep theirs."""
        cutoff = _cutoff if _cutoff is not None else self._invalidated_cutoff(older_than_seconds)
        db = self._get_db()
        if db is not None:
            try:
                db.execute(
                    "DELETE FROM chunk_vectors WHERE chunk_id IN "
                    "(SELECT id FROM chunks WHERE invalidated_at IS NOT NULL "
                    "AND invalidated_at <= ? AND invalidation_reason = ?)",
                    (cutoff, _BULK_DELETE_REASON),
                )
                db.commit()
            except sqlite3.DatabaseError as exc:
                log.warning("[knowledge] purge_invalidated vectors failed: %s", exc)
            finally:
                db.close()
        return super().purge_invalidated(older_than_seconds, _cutoff=cutoff)

    def _vector_search(
        self,
        query_vec: list[float],
        k: int,
        domain: str | None,
        namespace: str | list[str] | None = None,
        include_invalidated: bool = False,
        epoch: str | None = None,
    ) -> list[int]:
        """Return chunk ids ranked by cosine similarity (brute force)."""
        db = self._get_db()
        if db is None:
            return []
        where: list[str] = []
        params: list = []
        if not include_invalidated:
            where.append("c.invalidated_at IS NULL")
        if domain:
            where.append("c.domain = ?")
            params.append(domain)
        if epoch:
            where.append("c.epoch = ?")
            params.append(epoch)
        ns_sql, ns_params = _namespace_clause(namespace, col="c.namespace")
        if ns_sql:
            where.append(ns_sql)
            params.extend(ns_params)
        sql = (
            "SELECT v.chunk_id, v.vec FROM chunk_vectors v "
            "JOIN chunks c ON c.id = v.chunk_id WHERE " + " AND ".join(where)
            if where
            else "SELECT chunk_id, vec FROM chunk_vectors"
        )
        try:
            rows = db.execute(sql, params).fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            db.close()

        qn = math.sqrt(sum(x * x for x in query_vec)) or 1.0
        scored: list[tuple[float, int]] = []
        for r in rows:
            try:
                vec = json.loads(r["vec"])
            except (json.JSONDecodeError, TypeError):
                continue
            if len(vec) != len(query_vec):
                continue
            dot = sum(a * b for a, b in zip(query_vec, vec))
            vn = math.sqrt(sum(x * x for x in vec)) or 1.0
            scored.append((dot / (qn * vn), int(r["chunk_id"])))
        scored.sort(reverse=True)
        return [cid for _, cid in scored[:k]]

    # ── hybrid search ────────────────────────────────────────────────────────────

    def search(
        self,
        query,
        k: int = 5,
        *,
        domain: str | None = None,
        namespace: str | list[str] | None = None,
        include_invalidated: bool = False,
        epoch: str | None = None,
    ) -> list[dict]:
        """RRF-fuse the FTS5 ranking with a vector ranking.

        Falls back to pure FTS5 when embeddings are unavailable (no embed_fn,
        circuit open, or the query fails to embed) — same shape, never raises.
        ``namespace`` (ADR 0069 D3a) filters BOTH rankings, so a fused hit can
        never come from outside the requested scope. Superseded rows
        (``invalidated_at``, ADR 0069 D9) are likewise excluded from BOTH
        rankings by default; ``include_invalidated=True`` is the audit escape
        hatch. ``epoch`` (#1634) likewise filters BOTH rankings — an
        out-of-era chunk can't surface as a vector-only hit.
        """
        if not query or not query.strip():
            return []

        base = super().search(
            query,
            k=self._vector_k,
            domain=domain,
            namespace=namespace,
            include_invalidated=include_invalidated,
            epoch=epoch,
        )
        query_vec = self._embed(query)
        if query_vec is None:
            return base[:k]

        vec_ids = self._vector_search(query_vec, self._vector_k, domain, namespace, include_invalidated, epoch)
        if not vec_ids:
            return base[:k]

        # Reciprocal Rank Fusion over the two rankings, keyed by chunk id.
        scores: dict[int, float] = {}
        by_id: dict[int, dict] = {}
        for rank, item in enumerate(base):
            cid = item.get("id")
            if cid is None:
                continue
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (self._rrf_k + rank)
            by_id[cid] = item
        for rank, cid in enumerate(vec_ids):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (self._rrf_k + rank)

        ordered = sorted(scores, key=lambda c: scores[c], reverse=True)
        if self._min_score > 0:
            ordered = [cid for cid in ordered if scores[cid] >= self._min_score]
        ordered = ordered[:k]
        results: list[dict] = []
        for cid in ordered:
            item = by_id.get(cid) or self._hydrate_chunk(cid)
            if item is not None:
                item["score"] = round(scores[cid], 6)  # RRF fused relevance (#1043)
                results.append(item)
        return results

    def _hydrate_chunk(self, chunk_id: int) -> dict | None:
        """Build a result dict for a vector-only hit not in the FTS5 results."""
        db = self._get_db()
        if db is None:
            return None
        try:
            row = db.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        except sqlite3.DatabaseError:
            return None
        finally:
            db.close()
        if row is None:
            return None
        d = dict(row)
        preview = (d.get("heading") + ": " if d.get("heading") else "") + d.get("content", "")
        return {"table": "chunks", "preview": preview[: self._preview_chars], **d}
