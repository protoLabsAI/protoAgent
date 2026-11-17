"""Public-input and lineage boundary for derived readers (ADR 0131 §5, amended by
bd-jo3v6 — "Conversations in Work").

When a conversation has a **private partition** (operator steering and its private
replies kept out of the shared transcript), every reader that *derives* retained
shared material — the harvest summary, the compaction archive, the closed-chat digest
card, and extracted facts — must learn only from its **public** part. A later ordinary
turn can clear ``incognito`` while older private messages still sit in the checkpoint,
so protection cannot ride the thread-level ``incognito`` flag (#5403): it follows the
retained data and its derivations.

This module is the single input boundary those readers share: given the checkpoint
messages and the session's archive spans, it returns the public projection (private
messages dropped, non-public archive spans dropped) plus the lineage verdict that
decides whether fact extraction may run at all. Fail-closed: unknown or mixed
provenance is treated as not-public (the ADR's "fail-closed omission is preferable to
an unsupported privacy promise"). A conversation WITHOUT a private partition is
unaffected — every message and span is public, exactly as before.

FUTURE OWNER: the ADR 0125 memory epic (#4555) Phase 2–3 reworks the fact extractor
through the memory path (memories table, split retrieval). Keep this boundary as the
one place the public-input and lineage check lives, so that rework can keep it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Audience labels. ``PRIVATE_AUDIENCE`` is the same string the A2A console partition
# stamps (``a2a_impl.stores.PRIVATE_AUDIENCE``); it is repeated here as a literal rather
# than imported so ``graph`` keeps no dependency on ``a2a_impl``. Keep the two in sync.
PRIVATE_AUDIENCE = "human-operator"
PUBLIC_AUDIENCE = "public"

# Immutable source version stamped on every derived record (summary, closed-chat card,
# archive, extracted fact) so a later reader can tell which projection produced it and
# refuse records from an unsupported/older projection rather than guessing.
SOURCE_VERSION = 1

# ``additional_kwargs`` key a partitioned checkpoint stamps per message, and the column a
# HistoryStore archive row carries. Absent ⇒ unmarked (legacy): a message is public by
# construction (the partition keeps private messages out of the shared list), but an
# archive blob can mix audiences, so an unmarked archive is unknown, not assumed public.
AUDIENCE_KWARG = "audience"


def message_audience(message) -> str | None:
    """The audience a checkpoint message was accepted under, from its ``additional_kwargs``
    (``None`` when unmarked)."""
    kwargs = getattr(message, "additional_kwargs", None) or {}
    value = kwargs.get(AUDIENCE_KWARG)
    return value if isinstance(value, str) and value else None


def is_private_message(message) -> bool:
    """Whether a checkpoint message belongs to the private partition."""
    return message_audience(message) == PRIVATE_AUDIENCE


def entry_audience(entry) -> str | None:
    """The audience of a HistoryStore archive span (``None`` when unmarked/legacy)."""
    value = getattr(entry, "audience", None)
    return value if isinstance(value, str) and value else None


def public_messages(messages: list) -> list:
    """Drop private-partition messages; keep public and unmarked (legacy) ones.

    Removing the private messages is always safe — it can only make a derivation carry
    less — so this applies unconditionally. A conversation without a private partition
    has no private messages and is returned unchanged.
    """
    return [m for m in messages if not is_private_message(m)]


@dataclass(frozen=True)
class PublicProjection:
    """The public view of a conversation for a derived reader.

    ``messages``/``archives`` are the public checkpoint messages and public archive
    spans to summarize together. ``lineage`` is ``"clean"`` when the whole public input
    is verifiably public (no private partition at all, or a fully-labelled partition) and
    ``"mixed"`` when a private partition coexists with unknown/unlabelled spans that
    cannot be verified public. ``facts_ok`` gates fact extraction: only a clean lineage
    may extract (ADR 0131 §5 — "Disable extraction for unknown/mixed provenance").
    """

    messages: list
    archives: list
    lineage: str
    facts_ok: bool


def public_projection(messages: list, archives: list) -> PublicProjection:
    """Build the public projection and lineage verdict for the harvest/extractor boundary.

    ``archives`` are the session's archive spans (read *including* private ones, so a
    private archive is both detected and excluded here). A conversation with no private
    partition keeps every message and span — behaviour is unchanged. When a private
    partition is present, only explicitly-public archive spans feed a public summary, and
    any remaining unknown span (an unlabelled legacy message or archive) makes the lineage
    ``"mixed"`` and turns fact extraction off.
    """
    messages = list(messages or [])
    archives = list(archives or [])
    private_messages = any(is_private_message(m) for m in messages)
    private_archives = any(entry_audience(a) == PRIVATE_AUDIENCE for a in archives)
    partitioned = private_messages or private_archives

    public_msgs = public_messages(messages)
    if not partitioned:
        # No private partition: every span is public (public or legacy-unmarked), exactly
        # as the harvest read them before this feature. Lineage is clean.
        public_arcs = [a for a in archives if entry_audience(a) != PRIVATE_AUDIENCE]
        return PublicProjection(messages=public_msgs, archives=public_arcs, lineage="clean", facts_ok=True)

    # Private partition present → fail closed. Only spans explicitly marked public may feed
    # a public summary; unknown/unlabelled spans are excluded and poison the lineage.
    public_arcs = [a for a in archives if entry_audience(a) == PUBLIC_AUDIENCE]
    unknown_span = any(message_audience(m) is None for m in public_msgs) or any(
        entry_audience(a) is None for a in archives
    )
    lineage = "mixed" if unknown_span else "clean"
    return PublicProjection(messages=public_msgs, archives=public_arcs, lineage=lineage, facts_ok=lineage == "clean")


def is_shared_audience(audience: str | None) -> bool:
    """Whether a derived record's audience is *shared* (peer-readable). Public and legacy
    unmarked (``None``) records are shared; a private record is not. Used to keep a private
    fact from ever merging into or superseding a shared one (ADR 0131 §5)."""
    return audience != PRIVATE_AUDIENCE
