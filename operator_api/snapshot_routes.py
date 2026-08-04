"""Agent snapshot export route (ADR 0091 D1/D2, #2103).

`POST /api/agent/export` — download this agent's declarative, secret-free snapshot as a
zip: SOUL, secret-stripped config, pinned plugin set, MCP servers, skills. Not a backup —
no runtime history, no credentials (ADR 0091 rejected the raw instance-root dump).

Thin by design. All the judgement lives in ``graph.snapshot_op``, which is host-free and
unit-tested against fixture trees; this module only resolves the live instance's paths and
shapes the HTTP response. The layering contract also requires it: ``graph/`` may not import
``operator_api/``, so the exporter cannot reach for ``instance_paths()`` on its own.

**Two modes, because an export is meant to leave the machine.** ``dry_run: true`` returns
the review as JSON — what would be stripped, what the target must re-supply, what the
pattern sweep matched — with no bytes. Without it you get the zip, which carries the same
review inside as ``REVIEW.md`` so the disclosure can never be separated from the artifact.

`POST /api/agent/import` (ADR 0091 D3) is the mirror, with the mirrored hazard: import does
not leak, it **executes**. Applying a snapshot clones the plugin repos it names and enables
their code in-process, and the artifact is one someone handed you. So it has the same
two-phase shape for the opposite reason — without ``acknowledged`` it returns the PLAN
(every URL, every capability the config grants) and changes nothing; with it, it applies.
The console must show the plan before it sets that flag.

Auth: gated by the server-level ``/api/*`` bearer middleware like every operator route.
That matters more here than most — this endpoint reads the agent's entire configuration,
and while the response is secret-free by construction, the *shape* of an agent (its plugins,
its MCP servers, its persona) is not something to hand out unauthenticated.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Body, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("protoagent.operator_api")


MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def register_snapshot_routes(app) -> None:
    @app.post("/api/agent/export")
    async def _agent_export(
        dry_run: bool = Body(False, embed=True),
        include_knowledge: bool = Body(False, embed=True),
    ):
        """Export this agent as a secret-free snapshot zip (ADR 0091).

        ``{"dry_run": true}`` → JSON review only (no bytes), for a console to show before
        the operator commits to a download. Otherwise → the zip as an attachment, with the
        review summary echoed in ``X-Snapshot-Review`` and written inside as ``REVIEW.md``.

        ``include_knowledge`` (#2105) additionally carries the agent's knowledge as text.
        **Default off, and it changes what the artifact is:** a definition-only snapshot is
        publishable, one carrying knowledge is not — no credentials, possibly private
        content. The review says so in both the JSON and ``REVIEW.md``.
        """
        from graph.snapshot_op import build_snapshot
        from infra.paths import instance_paths

        paths = instance_paths()
        knowledge = await asyncio.to_thread(_collect_knowledge) if include_knowledge else None
        try:
            # Off-thread: reads config + SOUL + every SKILL.md and deflates a zip — small,
            # but all blocking I/O, and it must not stall the event loop mid-turn.
            result = await asyncio.to_thread(
                build_snapshot,
                config_yaml=paths.config_yaml,
                soul_path=paths.soul_path,
                plugins_lock=paths.plugins_lock,
                # read-only, for `was_set` only — the file itself can never become a zip member
                secrets_yaml=paths.secrets_yaml,
                knowledge=knowledge,
                skills_dirs={"instance": paths.skills_dir, "config": paths.config_dir / "skills"},
                # name defaults from the config's `identity.name` — see build_snapshot
            )
        except Exception as exc:  # noqa: BLE001 — never leak a stack trace to the console
            log.exception("[snapshot] export failed")
            raise HTTPException(status_code=500, detail=f"Snapshot export failed: {type(exc).__name__}") from exc

        if dry_run:
            return JSONResponse(result.summary())

        import json

        # The header is a convenience for a client that wants the review without opening
        # the zip; REVIEW.md inside is the copy that actually travels with the artifact.
        # Trimmed to names only so a long roster can't blow a header size limit.
        header = json.dumps(
            {
                "required_secrets": [r.name for r in result.required_secrets],
                "pattern_redactions": sorted(result.pattern_redactions),
                "notes": result.notes,
            }
        )
        return Response(
            content=result.data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{result.filename}"',
                "X-Snapshot-Review": header,
            },
        )

    @app.post("/api/agent/import")
    async def _agent_import(
        file: UploadFile = File(..., description="The snapshot zip"),
        name: str = Form("", description="Name for the new agent; blank uses the snapshot's"),
        acknowledged: bool = Form(False, description="The operator has read the plan and consents to running its plugin code"),
        secrets_json: str = Form("", description='JSON {"model.api_key": "…"} of supplied credentials'),
    ):
        """Inspect or apply an agent snapshot (ADR 0091 D3).

        **Without ``acknowledged`` this only inspects** — it returns the plan (plugins that
        would be installed and whether their source is familiar, capabilities the config
        grants, credentials needed) and writes nothing. Applying installs and runs code the
        snapshot names, so the console shows that plan and gets an explicit yes first.

        Returns the plan (inspect) or the import result (apply). 400 on a malformed or
        unsafe archive — traversal, zip-bomb, unsupported version — none of which reach disk.
        """
        import json

        from graph.snapshot_import import SnapshotError, apply_snapshot, inspect_snapshot

        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"snapshot exceeds {MAX_UPLOAD_BYTES} bytes")

        known = await asyncio.to_thread(_known_plugin_sources)
        try:
            plan = await asyncio.to_thread(inspect_snapshot, data, known_sources=known)
        except SnapshotError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not acknowledged:
            return JSONResponse({"mode": "plan", **plan.as_dict()})

        supplied: dict = {}
        if secrets_json.strip():
            try:
                parsed = json.loads(secrets_json)
                supplied = {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="secrets_json is not valid JSON") from exc

        try:
            result = await asyncio.to_thread(
                apply_snapshot,
                data,
                name=(name or "").strip() or plan.agent_name,
                acknowledged=True,
                secrets=supplied,
                plan=plan,
            )
        except SnapshotError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — never leak a stack trace to the console
            log.exception("[snapshot] import failed")
            raise HTTPException(status_code=500, detail=f"Import failed: {type(exc).__name__}: {exc}") from exc
        return JSONResponse({"mode": "applied", **result.as_dict()})


def _collect_knowledge():
    """This instance's knowledge as an exportable seed, or None when there's no store.
    Off the loop by the caller — reads every chunk in the base."""
    from graph.snapshot_op import collect_knowledge_seed
    from runtime.state import STATE

    store = getattr(STATE, "knowledge_store", None)
    if store is None:
        return None
    try:
        return collect_knowledge_seed(store)
    except Exception:  # noqa: BLE001 — the seed is optional; never lose the export over it
        log.warning("[snapshot] knowledge seed collection failed", exc_info=True)
        return None


def _known_plugin_sources() -> list[str] | None:
    """`plugins.sources.allow`, used only to decide whether a pin's source is FAMILIAR.
    Advisory — it sharpens the console's warning, never gates the import."""
    try:
        from graph.plugins.installer import configured_allowlist

        return configured_allowlist()
    except Exception:  # noqa: BLE001 — familiarity is advisory
        return None
