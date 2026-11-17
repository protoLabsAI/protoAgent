"""Dormant public-projection boundary for derived conversation memory.

The trusted source reader is deliberately unbound in production in this proof
slice. It must return None only for a known unpartitioned conversation, and
raise for missing/unknown/mixed lineage. It must read retained public data, never
reconstruct it from a checkpoint. Future ADR 0125 retrieval can reuse this gate.
Source updates and publication must share session_gist.card_lock.
"""

from dataclasses import dataclass
from typing import Callable

from langchain_core.messages import BaseMessage


@dataclass(frozen=True)
class PublicSource:
    version: str
    messages: tuple[BaseMessage, ...]
    audience: str = "public"


# Trusted integration seam, not a config field or a graph-state input.
_source_reader: Callable[[str], PublicSource | None] | None = None
PROVENANCE = "public_memory_source"


def source_for(session_id: str) -> PublicSource | None:
    source = _source_reader(session_id) if _source_reader else None
    if source is not None and (
        not isinstance(source, PublicSource)
        or source.audience != "public"
        or not isinstance(source.version, str)
        or not source.version
        or not all(isinstance(message, BaseMessage) for message in source.messages)
    ):
        raise ValueError("Unknown public memory source")
    return source


def stamp(source: PublicSource) -> dict:
    return {"audience": "public", "version": source.version}


def public_card(card: dict) -> bool:
    """Revalidate a derived record against its current authoritative source.

    Unstamped legacy cards are supported only for unpartitioned conversations.
    A source failure never falls back to raw retained text.
    """
    if not isinstance(card, dict):
        return False
    try:
        source = source_for(str(card.get("session_id") or ""))
        return card.get(PROVENANCE) == stamp(source) if source else PROVENANCE not in card
    except Exception:
        return False


def public_state(state: dict, session_id: str) -> tuple[dict, dict]:
    source = source_for(session_id)
    if source is None:
        return state, {}
    return {**state, "messages": list(source.messages)}, {PROVENANCE: stamp(source)}
