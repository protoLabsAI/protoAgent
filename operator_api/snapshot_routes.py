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

Auth: gated by the server-level ``/api/*`` bearer middleware like every operator route.
That matters more here than most — this endpoint reads the agent's entire configuration,
and while the response is secret-free by construction, the *shape* of an agent (its plugins,
its MCP servers, its persona) is not something to hand out unauthenticated.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Body, HTTPException
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("protoagent.operator_api")


def register_snapshot_routes(app) -> None:
    @app.post("/api/agent/export")
    async def _agent_export(dry_run: bool = Body(False, embed=True)):
        """Export this agent as a secret-free snapshot zip (ADR 0091).

        ``{"dry_run": true}`` → JSON review only (no bytes), for a console to show before
        the operator commits to a download. Otherwise → the zip as an attachment, with the
        review summary echoed in ``X-Snapshot-Review`` and written inside as ``REVIEW.md``.
        """
        from graph.snapshot_op import build_snapshot
        from infra.paths import instance_paths

        paths = instance_paths()
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
