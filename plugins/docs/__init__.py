"""Docs plugin — let the agent answer questions about protoAgent from its own docs.

Ships a keyword FTS index over the bundled `docs/` tree (built in memory at load) plus two
tools — `docs_search` (find the right page) and `docs_read` (read its markdown) — and a
SKILL.md (`skills/answering-docs.md`, auto-discovered) that teaches search → read → cite.
First-party, `enabled: true`. A console Docs reader view + ⌘K search come in a follow-up.

No knowledge-store coupling and no embeddings: the index is self-contained and offline, so
docs Q&A works in the frozen desktop app and never pollutes the operator's knowledge store.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.tools import tool

from .corpus import read_doc
from .docs_index import DocsIndex

log = logging.getLogger("protoagent.plugins.docs")

_INDEX: DocsIndex | None = None


def _index() -> DocsIndex:
    """The process-wide docs index, built lazily on first use."""
    global _INDEX
    if _INDEX is None:
        idx = DocsIndex()
        try:
            n = idx.seed()
            log.info("[docs] indexed %d doc(s)", n)
        except Exception as exc:  # noqa: BLE001 — never let a bad corpus break the tools
            log.warning("[docs] index seed failed: %s", exc)
        _INDEX = idx
    return _INDEX


@tool
async def docs_search(query: str, k: int = 5) -> str:
    """Search the protoAgent project documentation for pages matching ``query``.

    Use this FIRST whenever the user asks how protoAgent works, or about a specific
    feature, configuration option, tool, plugin, API, or design decision (ADR) — anything
    answerable from the docs. Returns the top matches as ``[section] Title — path`` lines;
    then call ``docs_read(path)`` on the best one or two.
    """
    k = max(1, min(int(k), 10))
    results = await asyncio.to_thread(_index().search, query, k)
    if not results:
        return "No matching docs."
    return "\n".join(f"[{r.section}] {r.title} — {r.path}" for r in results)


@tool
async def docs_read(path: str) -> str:
    """Read the full markdown of a protoAgent doc by its ``path`` (e.g. ``guides/skills.md``,
    as returned by ``docs_search``). Answer from what you read and **cite the path**."""
    if not _index().has(path):
        return f"No such doc: {path!r}. Use docs_search to find the right path."
    text = await asyncio.to_thread(read_doc, path)
    return text or f"Could not read {path!r}."


def register(registry) -> None:
    """Entry point — build the index (so the first turn is fast) + expose the tools.
    The ``skills/`` dir is auto-discovered by the loader; no explicit registration."""
    try:
        _index()
    except Exception as exc:  # noqa: BLE001 — plugin load must never fail on this
        log.warning("[docs] index build at load failed: %s", exc)
    registry.register_tools([docs_search, docs_read])
