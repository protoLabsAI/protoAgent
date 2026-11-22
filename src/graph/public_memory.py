"""Public-projection boundary for derived conversation readers.

The trusted source reader returns None only for a known unpartitioned conversation,
and raises for missing/unknown/mixed lineage. It must read retained public data, never
reconstruct it from a checkpoint. Future ADR 0125 retrieval can reuse this gate.
Source updates and publication must share session_gist.card_lock.

The production reader (:func:`production_source`, the module default) knows one thing: a
conversation that holds a private steer has no verified public version, so it yields a
source with no messages and ``traceable=False`` — tracing, prompt capture and the fleet
trace export then record no content for it (#5403).
"""

import hashlib

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


def production_source(session_id: str) -> PublicSource | None:
    """The production source reader: what is known about ``session_id``'s private partition.

    A conversation holding a private steer — the mark and the operator-owned task the
    private-steer path writes (``graph.private_sessions``), or the private turn running
    right now — returns a ``PublicSource`` with no messages, no fleet snapshot and
    ``traceable=False``: nothing of it has been verified public. A conversation with no
    private partition returns None and is read exactly as before. When ownership cannot be
    read the lineage is unknown and this raises
    (:class:`graph.private_sessions.OwnershipUnknown`), so every caller fails closed.
    """
    from observability import tracing

    from graph import private_sessions
    from graph.fence_scope import in_private_segment

    session_id = str(session_id or "")
    running = in_private_segment() and session_id == (tracing.current_session_id() or "")
    if not session_id and not running:
        return None  # no conversation (background work, a route): nothing partitioned
    if not (
        running
        or private_sessions.is_marked(session_id)
        or private_sessions._owned_private_turn(session_id)
    ):
        return None
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return PublicSource(version=f"private-steer:{digest}", messages=())


# Trusted integration seam, not a config field or a graph-state input.
_source_reader: Callable[[str], PublicSource | None] | None = production_source
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
    material (#5403). In production the reader is :func:`production_source`: a conversation
    holding a private steer is never traceable, and one with none reads unpartitioned.
    """
    try:
        source = source_for(session_id)
    except Exception:
        return False
    return source.traceable if source is not None else True


def _memory_source(session_id: str) -> PublicSource | None:
    """The source the session-memory readers (``public_card``/``public_state``) gate on.

    The production reader's verdict is an observability one — "record no content". Session
    memory already has its own private-steer guards (``private_sessions.withheld`` on the
    writers, ``hidden_from_caller`` on the readers), which keep a privately steered
    conversation's card readable by the human operator; so under the production default
    these readers stay unpartitioned, exactly as before. Any other bound reader gates them.
    """
    return None if _source_reader is production_source else source_for(session_id)


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
        source = _memory_source(str(card.get("session_id") or ""))
        return card.get(PROVENANCE) == stamp(source) if source else PROVENANCE not in card
    except Exception:
        return False


def public_state(state: dict, session_id: str) -> tuple[dict, dict]:
    source = _memory_source(session_id)
    if source is None:
        return state, {}
    return {**state, "messages": list(source.messages)}, {PROVENANCE: stamp(source)}
