"""ADR 0125 P2-G1 (D7): the shared operator profile store (``<commons>/profile.db``).

A :class:`~knowledge.memories.MemoryStore` opened on the SAME host-shared commons root
the knowledge commons uses (ADR 0041) — un-scoped, so every agent on the box that OPTS IN
(``memory.shared_profile``) reads one profile DB regardless of ``instance.id``. The path is
taken verbatim (``scoped=False``), exactly like the commons knowledge store, so the private
``KNOWLEDGE_DB_PATH`` override never redirects it.

Only ``kind in (profile, standing)`` is accepted — the operator's standing identity and
preferences (name, timezone, how they like to be addressed), not the per-session ``fact`` /
``decision`` rows that belong in the private ``agent.db``. Agent writes land
``review_state='pending'`` and operator writes start ``'confirmed'`` — inherited unchanged
from :meth:`MemoryStore.add`'s provenance default. Delivery into the prompt and the tool flag
that lets the agent write here are P2-G2.
"""

from __future__ import annotations

import threading
from contextlib import closing
from pathlib import Path

from knowledge.hybrid_store import EmbedFn
from knowledge.memories import _SCHEMA, MemoryStore
from knowledge.store import _resolve_path

# The ONLY kinds this store holds — standing operator identity/preferences, never the
# per-session facts/decisions that stay private in agent.db.
ALLOWED_KINDS = frozenset({"profile", "standing"})


class SharedProfileStore(MemoryStore):
    """A ``MemoryStore`` over the host-shared ``<commons>/profile.db`` (ADR 0041 / 0125 D7).

    Un-scoped like the knowledge commons. Rejects any ``kind`` outside
    :data:`ALLOWED_KINDS`; otherwise it is an ordinary :class:`MemoryStore`.
    """

    ALLOWED_KINDS = ALLOWED_KINDS

    def __init__(self, commons_dir: str | Path, *, embed_fn: EmbedFn | None = None):
        # Verbatim, un-scoped commons path (ADR 0041) — NOT the per-instance / env-overridden
        # resolution a private store uses; the profile DB is host-level and shared.
        path = Path(commons_dir).expanduser() / "profile.db"
        self.path = _resolve_path(str(path), scoped=False)
        self._embed_fn = embed_fn
        self._embed_lock = threading.Lock()
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)

    def add(self, content: str, *, kind: str, provenance: str, **kwargs) -> int:
        """Persist a profile/standing memory. Rejects any other ``kind`` with ``ValueError``.

        ``review_state`` is left to :meth:`MemoryStore.add`: agent writes default ``pending``,
        operator writes (``provenance='operator'``) default ``confirmed``.
        """
        if kind not in ALLOWED_KINDS:
            raise ValueError(
                f"SharedProfileStore accepts only kind in {sorted(ALLOWED_KINDS)}; got {kind!r}"
            )
        return super().add(content, kind=kind, provenance=provenance, **kwargs)
