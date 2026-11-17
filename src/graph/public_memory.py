"""Dormant public-projection boundary for derived conversation readers.

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
class FleetExport:
    """Public auxiliary snapshot covered by the enclosing source's lineage.

    The trusted reader supplies fields from the same retained public version and
    raises if any auxiliary input has missing, mixed or stale provenance.
    """

    task_id: str
    orient: str
    tools: list[dict]
    metadata: dict


@dataclass(frozen=True)
class PublicSource:
    version: str
    messages: tuple[BaseMessage, ...]
    audience: str = "public"
    # Optional for memory readers; required for a protected fleet export.
    fleet: FleetExport | None = None
    # P1g.1 observability boundary: True only once the trusted reader has verified this
    # retained version's composed input (messages + memory) is public and so may be traced
    # or prompt-captured. A partitioned conversation records no trace/capture content while
    # this is False — a later ordinary turn cannot clear it (see ``content_public``).
    traceable: bool = False


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


def content_public(session_id: str) -> bool:
    """Whether trace and prompt-capture CONTENT may be recorded for ``session_id`` (ADR
    0131 §5, amended by bd-jo3v6 — the P1g.1 observability boundary).

    True for a known unpartitioned conversation (``source_for`` returns None) — tracing and
    prompt capture are byte-for-byte unchanged without a private partition. A partitioned
    conversation records only content-free metrics until the trusted reader has verified
    this retained version's composed input is public (``PublicSource.traceable``); unknown,
    mixed or missing lineage fails closed (no content). This follows the retained source,
    not the turn's ``incognito`` flag, so a later ordinary turn cannot re-expose private
    material (#5403). Dormant in production: the source reader is unbound, so every
    conversation reads unpartitioned and this is True.
    """
    try:
        source = source_for(session_id)
    except Exception:
        return False
    return source.traceable if source is not None else True


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
