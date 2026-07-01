"""Trust tiers for knowledge-store rows (ADR 0069 D8).

Every chunk carries a ``source_type`` naming the write path that created it.
This module ranks those write paths into three deterministic trust tiers, so
the delivery layer can down-weight (or, via ``knowledge.inject_min_trust``,
exclude) low-trust content from the per-turn auto-injection while keeping it
reachable on demand through ``memory_recall`` — with the tier visible either
way.

The tiers (higher = more trusted):

- **3 — operator.** The operator wrote it deliberately through a console
  surface (knowledge browser add/edit, memory-inspector hot edit). These rows
  are direct operator intent.
- **2 — agent.** The agent derived it from conversation with the operator:
  extracted facts, harvest summaries, compaction archives, ``memory_ingest``
  notes. Trustworthy in the main, but a model can be induced to write them —
  the MINJA-style poisoning surface.
- **1 — external.** Ingested third-party content: web pages, YouTube
  transcripts, PDFs, transcribed media, pasted documents. The OWASP ASI06
  memory-poisoning surface — nothing here was authored by the operator or
  the agent.

An **unknown or missing** ``source_type`` maps to tier 1: a write path that
doesn't identify itself gets the least trust, not the benefit of the doubt
(this also covers rows written before source stamping existed, and plugin
SDK writes that don't stamp one).

The map is deliberately a code-level constant, not config: the tier of a
write path is a property of the architecture (who can reach that path), not
an operator preference. What IS config is the delivery policy built on it
(``knowledge.inject_min_trust``).
"""

from __future__ import annotations

# Lowest tier — unknown/unstamped source types land here (least trust by default).
DEFAULT_TRUST_TIER = 1

# source_type → tier. Keys are every value the in-tree writers stamp (audited
# 2026-07 across graph/, tools/, knowledge/, ingestion/, operator_api/) plus
# the generic aliases the ADR names (manual/harvest/web/ingest/external) so
# forks using those spellings rank sensibly too.
TRUST_TIERS: dict[str, int] = {
    # Tier 3 — operator-authored (console routes stamp "operator").
    "operator": 3,
    "manual": 3,
    # Tier 2 — agent-derived from conversation.
    "extracted": 2,  # graph/memory_facts.py — session-end fact extraction
    "harvest": 2,  # graph/conversation_harvest.py — retired-thread summaries
    "conversation": 2,  # memory_ingest tool + compaction archives
    "chat": 2,  # knowledge/store.add_finding default
    # Tier 1 — ingested / third-party content (ingestion/engine.py source types).
    "text": 1,
    "markdown": 1,
    "pdf": 1,
    "html": 1,
    "audio": 1,
    "video": 1,
    "image": 1,
    "youtube": 1,
    "web": 1,
    "ingest": 1,
    "external": 1,
}

# Tier → the short label shown in recall/injection lines. Kept to one word so
# the in-context cost is negligible.
TIER_LABELS: dict[int, str] = {3: "operator", 2: "agent", 1: "external"}


def trust_tier(source_type: str | None) -> int:
    """The trust tier for a row's ``source_type`` (deterministic — a plain
    dict lookup, case-insensitive, unknown/empty → ``DEFAULT_TRUST_TIER``)."""
    if not source_type:
        return DEFAULT_TRUST_TIER
    return TRUST_TIERS.get(str(source_type).strip().lower(), DEFAULT_TRUST_TIER)


def tier_label(tier: int) -> str:
    """Human label for a tier (unknown tiers clamp to the external label)."""
    return TIER_LABELS.get(int(tier), TIER_LABELS[DEFAULT_TRUST_TIER])


def trust_label(source_type: str | None) -> str:
    """Shorthand: the label for a ``source_type``'s tier."""
    return tier_label(trust_tier(source_type))
