"""Knowledge-store + playbooks routes for the operator console.

The console's "Knowledge" surface is a searchable Store + Playbooks (ADR 0020):
the FTS5 knowledge base (findings, daily-log, harvested sessions, operator notes)
and the procedural-memory skill index. Extracted from ``server._main`` (ADR 0023
phase 3) into a ``register_knowledge_routes(app)`` registrar. Browse/search is
best-effort and degrades to ``{"enabled": False}`` when its store is off; none of
the routes ever 500s the console. The chunk CRUD routes let the operator curate
the store directly (add a fact, fix a stale one, drop a wrong one) — the same
``KnowledgeBackend`` protocol surface every backend implements (ADR 0031).
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import File, Form, UploadFile
from fastapi.responses import JSONResponse

from runtime.state import STATE

log = logging.getLogger("protoagent.server")


# ── Playbooks (skills) helpers ────────────────────────────────────────────────
# Operator-authored skills are persisted as real SKILL.md files under the writable
# user-skills root (so they survive reboots + are exportable); the route layer
# composes that file layer (graph.skills.authoring) with the live SkillsIndex so a
# create/edit shows up without a restart.


def _user_skills_root():
    """The writable root for operator-authored ``SKILL.md`` skills. Not created
    here — only a write (``write_skill``) mkdirs it, so the read/list path is a
    pure lookup with no filesystem side effects."""
    from infra.paths import user_skills_dir

    return user_skills_dir()


def _as_str_list(value) -> list[str]:
    """Coerce a tools field (list, or comma/newline string) to a clean string list."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [t.strip() for t in re.split(r"[,\n]", value) if t.strip()]
    return []


def _find_skill(idx, skill_id: int, *, writable_only: bool = False):
    """Return the skill dict with rowid *skill_id*, or None. With ``writable_only``
    skip commons-tier rows — their rowids live in a separate DB and are read-only
    here, so an id never resolves to a shared skill for a write/delete."""
    for s in idx.all_skills():
        if s.get("id") != skill_id:
            continue
        if writable_only and s.get("tier") == "commons":
            continue
        return s
    return None


def _skill_response(idx, name: str, root):
    """The metadata row (no prompt_template) for the just-written skill *name*,
    tagged with origin/editable — the shape the list route returns."""
    from graph.skills.authoring import classify, slugify

    target = slugify(name)
    for s in idx.all_skills():
        if slugify(s.get("name", "")) == target and s.get("tier") != "commons":
            origin, editable = classify(s, root)
            row = {k: v for k, v in s.items() if k != "prompt_template"}
            row["origin"], row["editable"] = origin, editable
            return row
    return None


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
        # RRF relevance score on a hybrid store (#1043); null for unranked rows
        # (plain FTS store / list_chunks).
        "score": d.get("score"),
        # Tier (ADR 0041 / bd-2wu): "private" | "commons" — present only on a layered
        # store; null otherwise. Backs the console's tier badges + promote/unshare.
        "tier": d.get("tier"),
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
        # Drop the (potentially large) prompt_template from the list payload; the
        # table only needs metadata. Tag each row with origin/editable so the UI
        # knows which skills it can edit (user-authored + learned) vs which are
        # read-only (bundled examples, shared commons). Sort pinned-first, then by
        # confidence.
        from graph.skills.authoring import classify

        root = _user_skills_root()
        out = []
        for s in skills:
            origin, editable = classify(s, root)
            row = {k: v for k, v in s.items() if k != "prompt_template"}
            row["origin"], row["editable"] = origin, editable
            out.append(row)
        out.sort(key=lambda s: (s.get("source") != "disk", -(s.get("confidence") or 0)))
        return {"enabled": True, "playbooks": out}

    # Create an operator-authored skill: write a real SKILL.md under the user-skills
    # root (durable + exportable) and index it live so it works without a restart.
    @app.post("/api/playbooks")
    async def _api_playbook_create(body: dict | None = None):
        idx = STATE.skills_index
        if idx is None:
            return {"enabled": False, "id": None}
        body = body or {}
        name = str(body.get("name", "")).strip()
        description = str(body.get("description", "")).strip()
        prompt = str(body.get("prompt_template", body.get("body", ""))).strip()
        if not name or not description or not prompt:
            return JSONResponse({"detail": "name, description, and body are required"}, status_code=400)
        from graph.skills.authoring import slugify, write_skill

        slug = slugify(name)
        if not slug:
            return JSONResponse({"detail": "name must contain letters or digits"}, status_code=400)
        if any(slugify(s.get("name", "")) == slug for s in idx.all_skills()):
            return JSONResponse({"detail": f"a skill named “{name}” already exists"}, status_code=409)
        root = _user_skills_root()
        try:
            artifact = write_skill(
                root, name, description, prompt,
                tools=_as_str_list(body.get("tools") or body.get("tools_used")),
                user_facing=bool(body.get("user_facing", False)),
                slash=str(body.get("slash", "")).strip(),
                user_only=bool(body.get("user_only", False)),
            )
            idx.add_skill(artifact, source="disk")
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] create failed")
            return JSONResponse({"detail": f"create failed: {exc}"}, status_code=400)
        created = _skill_response(idx, name, root)
        return {"enabled": True, "id": (created or {}).get("id"), "skill": created}

    # Fetch one skill WITH its full prompt_template (the list omits it) so the editor
    # can pre-fill. Read-only — returns commons/bundled skills too (for viewing).
    @app.get("/api/playbooks/{skill_id}")
    async def _api_playbook_get(skill_id: int):
        idx = STATE.skills_index
        if idx is None:
            return JSONResponse({"detail": "skills index disabled"}, status_code=404)
        s = _find_skill(idx, skill_id)
        if s is None:
            return JSONResponse({"detail": "no skill with that id"}, status_code=404)
        from graph.skills.authoring import classify

        origin, editable = classify(s, _user_skills_root())
        return {"enabled": True, "skill": {**s, "origin": origin, "editable": editable}}

    # Edit a skill: rewrite its SKILL.md and re-index. Editing a learned (DB-only)
    # skill MATERIALIZES it as a durable user SKILL.md (curation = persistence).
    # Bundled examples + shared commons skills are read-only.
    @app.put("/api/playbooks/{skill_id}")
    async def _api_playbook_update(skill_id: int, body: dict | None = None):
        idx = STATE.skills_index
        if idx is None:
            return {"enabled": False, "id": None}
        root = _user_skills_root()
        from graph.skills.authoring import classify, remove_skill, slugify, write_skill

        cur = _find_skill(idx, skill_id, writable_only=True)
        if cur is None:
            return JSONResponse({"detail": "no editable skill with that id"}, status_code=404)
        origin, editable = classify(cur, root)
        if not editable:
            return JSONResponse({"detail": f"{origin} skills are read-only"}, status_code=403)
        body = body or {}
        name = str(body.get("name", cur.get("name", ""))).strip()
        description = str(body.get("description", "")).strip()
        prompt = str(body.get("prompt_template", body.get("body", ""))).strip()
        if not name or not description or not prompt:
            return JSONResponse({"detail": "name, description, and body are required"}, status_code=400)
        new_slug, old_slug = slugify(name), slugify(cur.get("name", ""))
        if new_slug != old_slug and any(
            slugify(s.get("name", "")) == new_slug and s.get("id") != skill_id for s in idx.all_skills()
        ):
            return JSONResponse({"detail": f"a skill named “{name}” already exists"}, status_code=409)
        try:
            artifact = write_skill(
                root, name, description, prompt,
                tools=_as_str_list(body.get("tools") or body.get("tools_used")),
                user_facing=bool(body.get("user_facing", cur.get("user_facing", False))),
                slash=str(body.get("slash", cur.get("slash", "") or "")).strip(),
                user_only=bool(body.get("user_only", cur.get("user_only", False))),
            )
            idx.delete_skill(skill_id)
            if new_slug != old_slug:
                remove_skill(root, cur.get("name", ""))  # renamed → drop the old folder
            idx.add_skill(artifact, source="disk")
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] update failed")
            return JSONResponse({"detail": f"update failed: {exc}"}, status_code=400)
        updated = _skill_response(idx, name, root)
        return {"enabled": True, "id": (updated or {}).get("id"), "skill": updated}

    @app.delete("/api/playbooks/{skill_id}")
    async def _api_playbook_delete(skill_id: int):
        idx = STATE.skills_index
        if idx is None:
            return {"enabled": False, "deleted": False}
        root = _user_skills_root()
        from graph.skills.authoring import classify, remove_skill

        cur = _find_skill(idx, skill_id, writable_only=True)
        if cur is None:
            return {"enabled": True, "deleted": False, "error": "no deletable skill with that id"}
        origin, _editable = classify(cur, root)
        if origin == "bundled":
            return {
                "enabled": True,
                "deleted": False,
                "error": "bundled example skills are read-only (edit their SKILL.md in config/skills)",
            }
        try:
            if origin == "user":
                remove_skill(root, cur.get("name", ""))  # drop the file too, not just the row
            idx.delete_skill(skill_id)
            return {"enabled": True, "deleted": True}
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] delete failed")
            return {"enabled": True, "deleted": False, "error": str(exc)}

    # Promote a private skill into the shared commons (ADR 0041 "shared brain,
    # private hands") — the one curated lift that makes the layered tier useful.
    # Only available when the index is layered (it has a ``promote`` method); in
    # scoped/shared mode there's a single library and nothing to promote into.
    # The id is the private-DB rowid, so resolve the name within the ``private``
    # tier (commons rows carry their own rowids — never promote those).
    @app.post("/api/playbooks/{skill_id}/promote")
    async def _api_playbook_promote(skill_id: int):
        idx = STATE.skills_index
        if idx is None:
            return {"enabled": False, "promoted": False}
        promote = getattr(idx, "promote", None)
        if promote is None:
            return {
                "enabled": True,
                "promoted": False,
                "error": "skills aren't in layered mode — set skills.scope: layered to share a commons.",
            }
        try:
            match = next(
                (s for s in idx.all_skills() if s.get("id") == skill_id and s.get("tier", "private") == "private"),
                None,
            )
            if match is None:
                return {"enabled": True, "promoted": False, "error": "no private skill with that id"}
            name = match.get("name", "")
            ok = bool(promote(name))
            return {"enabled": True, "promoted": ok, "name": name}
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] promote failed")
            return {"enabled": True, "promoted": False, "error": str(exc)}

    # Forget a skill FROM the shared commons (ADR 0041) — the inverse of promote and
    # the only console way to curate the commons (the curator writes private-only, so
    # promoted skills are otherwise CLI-only to remove). Layered-only; the id is a
    # COMMONS-DB rowid, so resolve the name within the ``commons`` tier and drop it.
    @app.post("/api/playbooks/{skill_id}/forget")
    async def _api_playbook_forget(skill_id: int):
        idx = STATE.skills_index
        if idx is None:
            return {"enabled": False, "forgotten": False}
        forget = getattr(idx, "forget_from_commons", None)
        if forget is None:
            return {
                "enabled": True,
                "forgotten": False,
                "error": "skills aren't in layered mode — there's no commons to forget from.",
            }
        try:
            match = next(
                (s for s in idx.all_skills() if s.get("id") == skill_id and s.get("tier") == "commons"),
                None,
            )
            if match is None:
                return {"enabled": True, "forgotten": False, "error": "no commons skill with that id"}
            name = match.get("name", "")
            ok = bool(forget(name))
            return {"enabled": True, "forgotten": ok, "name": name}
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] forget failed")
            return {"enabled": True, "forgotten": False, "error": str(exc)}

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
                rows = await asyncio.to_thread(STATE.knowledge_store.search, q, k=k, domain=domain or None)
                results = [_knowledge_row(r) for r in rows]
            else:
                # list_chunks yields Chunk objects (plain store) or tier-tagged dicts
                # (LayeredKnowledgeStore) — normalize either.
                results = [
                    _knowledge_row(c if isinstance(c, dict) else c.as_dict())
                    for c in STATE.knowledge_store.list_chunks(domain=domain or None, limit=k)
                ]
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[knowledge] search failed")
        try:
            stats = STATE.knowledge_store.stats()
        except Exception:  # noqa: BLE001
            stats = {}
        return {"enabled": True, "query": q, "results": results, "stats": stats}

    # --- Knowledge commons promote/forget (ADR 0041 / bd-2wu) ----------------
    # The curated lift into the shared commons + its inverse. Only meaningful when
    # the store is layered (it has promote/forget_from_commons); otherwise there's a
    # single store and nothing to promote into — report a hint, mirroring playbooks.
    @app.post("/api/knowledge/{chunk_id}/promote")
    async def _api_knowledge_promote(chunk_id: int):
        store = STATE.knowledge_store
        if store is None:
            return {"enabled": False, "promoted": False}
        promote = getattr(store, "promote", None)
        if promote is None:
            return {
                "enabled": True,
                "promoted": False,
                "error": "knowledge isn't in layered mode — set knowledge.scope: layered to share a commons.",
            }
        try:
            rec = await asyncio.to_thread(promote, chunk_id)
            return {"enabled": True, "promoted": rec is not None}
        except Exception as exc:  # noqa: BLE001
            log.exception("[knowledge] promote failed")
            return {"enabled": True, "promoted": False, "error": str(exc)}

    @app.post("/api/knowledge/{chunk_id}/forget")
    async def _api_knowledge_forget(chunk_id: int):
        store = STATE.knowledge_store
        if store is None:
            return {"enabled": False, "forgotten": False}
        forget = getattr(store, "forget_from_commons", None)
        if forget is None:
            return {
                "enabled": True,
                "forgotten": False,
                "error": "knowledge isn't in layered mode — there's no commons to forget from.",
            }
        try:
            ok = await asyncio.to_thread(forget, chunk_id)
            return {"enabled": True, "forgotten": bool(ok)}
        except Exception as exc:  # noqa: BLE001
            log.exception("[knowledge] forget failed")
            return {"enabled": True, "forgotten": False, "error": str(exc)}

    # --- Knowledge chunk CRUD (operator curation) ---------------------------
    # The store fills up with harvested sessions / findings the operator could
    # only read; these let them curate it: add a fact, fix a stale one, drop a
    # wrong one. add/delete are the KnowledgeBackend protocol (ADR 0031); edit
    # composes them (add the new revision FIRST, then delete the old — a failed
    # add must never lose the original) so it works on every backend, and a
    # hybrid store re-embeds the new content on the way in.

    @app.post("/api/knowledge/chunks")
    async def _api_knowledge_add(body: dict | None = None):
        if STATE.knowledge_store is None:
            return {"enabled": False, "id": None}
        body = body or {}
        content = str(body.get("content", "")).strip()
        if not content:
            return JSONResponse({"detail": "content is required"}, status_code=400)
        # add_document chunks a large paste into per-passage embeddings (ADR 0021)
        # and is a no-op split for a short fact; degrades to one add_chunk on a
        # plugin backend that only implements the ADR 0031 surface.
        from knowledge import add_document

        ids = await asyncio.to_thread(
            lambda: add_document(
                STATE.knowledge_store,
                content,
                domain=str(body.get("domain", "") or "general"),
                heading=(str(body.get("heading", "")).strip() or None),
                source="console",
                source_type="operator",
            )
        )
        if not ids:
            return JSONResponse({"detail": "the store rejected the chunk"}, status_code=400)
        return {"enabled": True, "id": ids[0], "ids": ids}

    @app.post("/api/knowledge/ingest")
    async def _api_knowledge_ingest(
        file: UploadFile | None = File(default=None),
        url: str = Form(default=""),
        text: str = Form(default=""),
        title: str = Form(default=""),
        domain: str = Form(default="general"),
    ):
        """Ingest a document (file / URL / pasted text) into the knowledge base.

        The ingestion engine turns the source into text (txt/md/html/pdf, audio +
        video via gateway STT, web + YouTube URLs), then ``add_document`` chunks +
        contextually enriches + embeds it (ADR 0021) — so a whole PDF, article, or
        recording becomes per-passage recall, not one diluted chunk. Multipart so
        a file upload and the URL/text fields share one endpoint. Extraction +
        embedding run off the event loop. Returns the created chunk ids."""
        if STATE.knowledge_store is None:
            return {"enabled": False, "ids": []}
        from ingestion import (
            ExtractResult,
            MissingDependency,
            UnsupportedSource,
            extract_bytes,
            extract_url,
        )
        from knowledge import add_document

        # Gateway STT for audio/video (None if no transcribe_model configured →
        # audio/video raise a clean "not configured" error, text/pdf unaffected).
        transcribe = None
        try:
            from graph.llm import create_transcribe_fn

            transcribe = create_transcribe_fn(STATE.graph_config) if STATE.graph_config else None
        except Exception as exc:  # noqa: BLE001 — transcription stays optional
            log.warning("[knowledge] transcribe fn unavailable: %s", exc)

        url, text, title = url.strip(), text.strip(), title.strip()
        source = "console"
        try:
            if file is not None:
                data = await file.read()
                result = await asyncio.to_thread(
                    extract_bytes, file.filename or "upload", data, file.content_type, transcribe=transcribe
                )
                source = file.filename or "upload"
            elif url:
                result = await asyncio.to_thread(extract_url, url, transcribe=transcribe)
                source = url
            elif text:
                result = ExtractResult(text=text, title=title or None, source_type="text")
            else:
                return JSONResponse({"detail": "provide a file, url, or text"}, status_code=400)
        except MissingDependency as exc:
            return JSONResponse({"detail": str(exc)}, status_code=501)
        except UnsupportedSource as exc:
            return JSONResponse({"detail": str(exc)}, status_code=415)
        except Exception as exc:  # noqa: BLE001 — surface extraction failure, never 500
            log.warning("[knowledge] ingest extraction failed: %s", exc)
            return JSONResponse({"detail": f"extraction failed: {exc}"}, status_code=400)

        heading = title or result.title or None
        ids = await asyncio.to_thread(
            lambda: add_document(
                STATE.knowledge_store,
                result.text,
                domain=(domain.strip() or "general"),
                heading=heading,
                source=source,
                source_type=result.source_type,
            )
        )
        if not ids:
            return JSONResponse({"detail": "nothing ingested (no text after extraction)"}, status_code=400)
        return {
            "enabled": True,
            "ids": ids,
            "chunks": len(ids),
            "title": heading,
            "source_type": result.source_type,
            "chars": len(result.text),
        }

    @app.post("/api/knowledge/attach")
    async def _api_chat_attach(
        file: UploadFile = File(...),
        session_id: str = Form(...),
    ):
        """Extract a chat attachment and TIER it so a big doc never gets dumped
        into the turn (ADR 0021):

        - text at or under ``knowledge.attach_inline_budget`` → inlined whole
          (``mode=inline``); the caller prepends ``context`` to its message.
        - a larger doc → ingested (chunked / contextually enriched / embedded)
          under a per-session namespace so the user's *question* retrieves the
          relevant passages, and only a ``lede`` is inlined as an anchor
          (``mode=indexed``). Cleaned up when the chat session is deleted.

        Returns the ready-to-prepend ``context`` block + a descriptor for the
        composer chip."""
        if STATE.knowledge_store is None:
            return {"enabled": False}
        from ingestion import MissingDependency, UnsupportedSource, extract_bytes
        from knowledge import add_document

        sid = (session_id or "").strip()
        if not sid:
            return JSONResponse({"detail": "session_id is required"}, status_code=400)

        transcribe = None
        describe = None
        try:
            from graph.llm import create_describe_image_fn, create_transcribe_fn

            if STATE.graph_config:
                transcribe = create_transcribe_fn(STATE.graph_config)
                # Image-describe (#1381): lets a text-only chat model "see" an attached image
                # via a configured vision model. None when no describe model is set.
                describe = create_describe_image_fn(STATE.graph_config)
        except Exception as exc:  # noqa: BLE001 — transcription/description stays optional
            log.warning("[knowledge] media fn unavailable: %s", exc)

        name = file.filename or "attachment"
        try:
            data = await file.read()
            result = await asyncio.to_thread(
                extract_bytes, name, data, file.content_type, transcribe=transcribe, describe=describe
            )
        except MissingDependency as exc:
            return JSONResponse({"detail": str(exc)}, status_code=501)
        except UnsupportedSource as exc:
            return JSONResponse({"detail": str(exc)}, status_code=415)
        except Exception as exc:  # noqa: BLE001 — surface extraction failure, never 500
            log.warning("[knowledge] attach extraction failed: %s", exc)
            return JSONResponse({"detail": f"extraction failed: {exc}"}, status_code=400)

        text = result.text
        budget = int(getattr(STATE.graph_config, "knowledge_attach_inline_budget", 8000) or 8000)

        if len(text) <= budget:
            context = f"[Attached file: {name}]\n{text}\n[end of {name}]"
            return {
                "enabled": True,
                "mode": "inline",
                "name": name,
                "source_type": result.source_type,
                "chars": len(text),
                "chunks": 0,
                "context": context,
            }

        # Large → index for retrieval (session-scoped, ephemeral) + inline a lede.
        ids = await asyncio.to_thread(
            lambda: add_document(
                STATE.knowledge_store,
                text,
                domain="attachment",
                namespace=f"attach:{sid}",
                source=name,
                source_type=result.source_type,
            )
        )
        lede = text[:budget]
        context = (
            f"[Attached file: {name} — large ({len(text)} chars), indexed for retrieval. "
            f"Opening excerpt:]\n{lede}\n"
            f"[Ask about its contents to retrieve more from {name}.]"
        )
        return {
            "enabled": True,
            "mode": "indexed",
            "name": name,
            "source_type": result.source_type,
            "chars": len(text),
            "chunks": len(ids),
            "context": context,
        }

    @app.put("/api/knowledge/chunks/{chunk_id}")
    async def _api_knowledge_update(chunk_id: int, body: dict | None = None):
        if STATE.knowledge_store is None:
            return {"enabled": False, "id": None}
        body = body or {}
        content = str(body.get("content", "")).strip()
        if not content:
            return JSONResponse({"detail": "content is required"}, status_code=400)
        new_id = await asyncio.to_thread(
            lambda: STATE.knowledge_store.add_chunk(
                content,
                str(body.get("domain", "") or "general"),
                heading=(str(body.get("heading", "")).strip() or None),
                source=(body.get("source") or "console"),
                source_type="operator",
            )
        )
        if new_id is None:
            return JSONResponse({"detail": "the store rejected the new revision"}, status_code=400)
        deleted = await asyncio.to_thread(STATE.knowledge_store.delete_by_id, chunk_id)
        if not deleted:
            log.warning("[knowledge] edit of chunk %s left the old row (delete failed)", chunk_id)
        return {"enabled": True, "id": new_id, "replaced": deleted}

    @app.delete("/api/knowledge/chunks/{chunk_id}")
    async def _api_knowledge_delete(chunk_id: int):
        if STATE.knowledge_store is None:
            return {"enabled": False, "deleted": False}
        deleted = await asyncio.to_thread(STATE.knowledge_store.delete_by_id, chunk_id)
        return {"enabled": True, "deleted": bool(deleted)}
