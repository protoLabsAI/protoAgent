"""External secrets-manager routes (ADR 0080) — ``/api/secrets/*``.

The console Settings panel's status/sync/test surface over ``infra.secrets``:
``GET /api/secrets/status`` (last fetch outcome + owned env-var names — names, never
values), ``POST /api/secrets/sync`` (force a refresh now), ``POST /api/secrets/test``
(fetch-only connection test, optionally with unsaved form overrides; applies nothing).
Gated by the ``/api/*`` operator bearer (a2a_impl/auth.py) like every operator route.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel


class SecretsTestRequest(BaseModel):
    """Unsaved form values to test with — every field optional; blank falls back to
    the saved config (and, for credentials, secrets.yaml → env)."""

    provider: str = ""
    host: str = ""
    project_id: str = ""
    environment: str = ""
    path: str = ""
    recursive: bool | None = None
    client_id: str = ""
    client_secret: str = ""
    timeout_seconds: float | None = None


def register_secrets_routes(app) -> None:
    from fastapi import APIRouter, HTTPException

    router = APIRouter()

    @router.get("/api/secrets/status")
    async def _secrets_status() -> dict:
        """Last hydration outcome + the env-var names currently owned by the
        orchestrator (provenance for the Settings panel). Secret values are never
        part of this surface."""
        from infra.secrets import status

        return status()

    @router.post("/api/secrets/sync")
    async def _secrets_sync() -> dict:
        """Force a fetch-and-apply now (bypasses the TTL gate) — the panel's
        "Sync now" button and the post-rotation nudge."""
        from graph.config import load_config_docs
        from graph.config_load import ConfigLoadError
        from graph.config_io import config_yaml_path
        from infra.secrets import SecretsRequiredError, hydrate_from_docs, status

        def _sync():
            merged, secrets_doc = load_config_docs(config_yaml_path(), validate=True)
            return hydrate_from_docs(merged, secrets_doc, force=True)

        try:
            result = await asyncio.to_thread(_sync)
        except ConfigLoadError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except SecretsRequiredError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except Exception:  # noqa: BLE001 — safe boundary for unexpected provider failures
            logging.getLogger(__name__).warning("Secrets sync failed unexpectedly")
            raise HTTPException(status_code=500, detail="secrets sync failed unexpectedly") from None
        if result is None:
            raise HTTPException(
                status_code=400,
                detail="secrets manager is not enabled (secrets_manager.enabled) or hydration "
                "is disabled (PROTOAGENT_NO_SECRETS_HYDRATE)",
            )
        return status()

    @router.post("/api/secrets/test")
    async def _secrets_test(req: SecretsTestRequest) -> dict:
        """Connection test: fetch with the saved config overlaid by any non-blank
        form values, WITHOUT applying anything to the environment. Returns the
        outcome + how many secrets the scope would yield (names capped, no values)."""
        import dataclasses

        from graph.config import load_config_docs
        from graph.config_io import config_yaml_path
        from graph.config_load import ConfigLoadError
        from infra.secrets import FetchResult, SourceConfig, get_provider, source_from_docs

        def _probe():
            merged, secrets_doc = load_config_docs(config_yaml_path())
            # Test must work before the operator flips `enabled` on, so build the
            # source from a copy of the docs with the gate forced open.
            merged = dict(merged or {})
            merged["secrets_manager"] = dict(merged.get("secrets_manager") or {})
            merged["secrets_manager"]["enabled"] = True
            cfg = source_from_docs(merged, secrets_doc) or SourceConfig()
            overrides = {
                k: v
                for k, v in {
                    "provider": req.provider.strip(),
                    "host": req.host.strip(),
                    "project_id": req.project_id.strip(),
                    "environment": req.environment.strip(),
                    "path": req.path.strip(),
                    "recursive": req.recursive,
                    "client_id": req.client_id.strip(),
                    # Credentials are opaque: whitespace may be intentional.
                    "client_secret": req.client_secret,
                    "timeout_seconds": req.timeout_seconds,
                }.items()
                if v not in ("", None)
            }
            # The saved credential belongs to the saved destination. Pointing the test at
            # another host or provider must bring its own secret, or the saved one would be
            # sent to wherever the form says.
            moved = any(k in overrides and overrides[k] != getattr(cfg, k) for k in ("host", "provider"))
            if moved and "client_secret" not in overrides and cfg.client_secret:
                return {
                    "ok": False,
                    "error": "enter the client secret to test against a different host or provider",
                    "error_kind": "not_configured",
                    "count": 0,
                    "names": [],
                }
            cfg = dataclasses.replace(cfg, **overrides)
            provider = get_provider(cfg.provider)
            if provider is None:
                return {"ok": False, "error": f"unknown provider {cfg.provider!r}", "error_kind": "not_configured"}
            try:
                result = provider.fetch(cfg)
            except Exception:  # noqa: BLE001 — contain a provider violating the never-raise contract
                result = None
            if not isinstance(result, FetchResult):
                # Exception text/tracebacks may include credentials or response bodies.
                logging.getLogger(__name__).warning("Secrets connection test: provider violated the fetch result contract")
                return {"ok": False, "error": "secrets provider failed during connection test",
                        "error_kind": "internal", "count": 0, "names": []}
            names = sorted(result.values or {}) if result.ok else []
            return {
                "ok": result.ok,
                "error": result.error,
                "error_kind": result.error_kind.value if result.error_kind else "",
                "count": len(names),
                "names": names[:100],
            }

        try:
            return await asyncio.to_thread(_probe)
        except ConfigLoadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid secrets connection configuration") from None
        except Exception:  # noqa: BLE001 — never echo arbitrary provider/configuration values
            logging.getLogger(__name__).warning("Secrets connection test failed unexpectedly")
            raise HTTPException(status_code=500, detail="secrets connection test failed unexpectedly") from None

    app.include_router(router)
