"""Knowledge-store + playbooks routes for the operator console.

The console's "Knowledge" surface is a searchable Store + Playbooks (ADR 0020):
the FTS5 knowledge base (findings, daily-log, harvested sessions, operator notes)
and the procedural-memory skill index. Extracted from ``server._main`` (ADR 0023
phase 3) into a ``register_knowledge_routes(app)`` registrar. Every route is
read-only / best-effort and degrades to ``{"enabled": False}`` when its store is
off; none ever 500s the console.
"""

from __future__ import annotations

import asyncio
import logging

from runtime.state import STATE

log = logging.getLogger("protoagent.server")


def _knowledge_row(d: dict) -> dict:
    """Normalize a search()/list_chunks() row to the console's shape."""
    heading = d.get("heading") or ""
    content = d.get("content") or ""
    preview = d.get("preview") or ((heading + ": " if heading else "") + content)[:240]
    return {
        "id": d.get("id"),
        "heading": heading,
        "content": content,
        "preview": preview,
        "domain": d.get("domain") or "general",
        "source": d.get("source"),
        "source_type": d.get("source_type"),
        "finding_type": d.get("finding_type"),
        "created_at": d.get("created_at"),
    }


def register_knowledge_routes(app) -> None:
    """Register the ``/api/playbooks*`` + ``/api/knowledge/search`` routes."""

    # --- Playbooks (skills surface, ADR 0009) ------------------------------
    # Browse + manage the procedural-memory skill index (skills.db) the operator
    # was otherwise blind to. "Playbooks" is the operator-facing name for the
    # skill-v1 artifacts (disk = pinned SKILL.md, emitted = agent-learned).
    @app.get("/api/playbooks")
    async def _api_playbooks():
        if STATE.skills_index is None:
            return {"enabled": False, "playbooks": []}
        try:
            skills = STATE.skills_index.all_skills()
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[playbooks] all_skills failed")
            return {"enabled": True, "playbooks": []}
        # Drop the (potentially large) prompt_template from the list payload;
        # the table only needs metadata. Sort pinned-first, then by confidence.
        out = [
            {k: v for k, v in s.items() if k != "prompt_template"}
            for s in skills
        ]
        out.sort(key=lambda s: (s.get("source") != "disk", -(s.get("confidence") or 0)))
        return {"enabled": True, "playbooks": out}

    @app.delete("/api/playbooks/{skill_id}")
    async def _api_playbook_delete(skill_id: int):
        if STATE.skills_index is None:
            return {"enabled": False, "deleted": False}
        try:
            STATE.skills_index.delete_skill(skill_id)
            return {"enabled": True, "deleted": True}
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] delete failed")
            return {"enabled": True, "deleted": False, "error": str(exc)}

    # --- Knowledge store (ADR 0020) ----------------------------------------
    # Searchable view of the agent's knowledge base (knowledge/store.py, FTS5):
    # findings, daily-log entries, harvested sessions, operator notes — the same
    # store KnowledgeMiddleware queries before each turn. An empty ``q`` returns
    # the most-recent chunks (a browsable default); a non-empty ``q`` runs FTS5
    # search. Read-only; never 500s the console.
    @app.get("/api/knowledge/search")
    async def _api_knowledge_search(q: str = "", k: int = 30, domain: str | None = None):
        if STATE.knowledge_store is None:
            return {"enabled": False, "query": q, "results": [], "stats": {}}
        results: list[dict] = []
        try:
            if q and q.strip():
                # search() embeds the query over HTTP on hybrid stores — run it
                # off the event loop (same pattern as graph/checkpointer.py).
                rows = await asyncio.to_thread(
                    STATE.knowledge_store.search, q, k=k, domain=domain or None
                )
                results = [_knowledge_row(r) for r in rows]
            else:
                results = [_knowledge_row(c.as_dict()) for c in STATE.knowledge_store.list_chunks(domain=domain or None, limit=k)]
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[knowledge] search failed")
        try:
            stats = STATE.knowledge_store.stats()
        except Exception:  # noqa: BLE001
            stats = {}
        return {"enabled": True, "query": q, "results": results, "stats": stats}
