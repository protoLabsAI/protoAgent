"""Live config / setup-wizard / settings routes for the operator console.

The `/api/config*` + `/api/settings*` surface: read/patch the live config + SOUL,
probe + test the LLM gateway, drive the setup wizard, and apply schema-driven
settings edits. Extracted from ``server._main`` (ADR 0023 phase 3) into a
``register_config_routes(app)`` registrar.

Config-changing routes offload to a worker thread (#497): applying settings
recompiles the graph, which would otherwise freeze the event loop. The apply /
finish-setup logic lives in ``server.agent_init``; these handlers are the thin
HTTP layer over it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from runtime.state import STATE
from server.agent_init import (
    _apply_settings_changes,
    _build_settings_callbacks,
    _reset_settings_keys,
)


class ConfigReloadRequest(BaseModel):
    config: dict | None = None
    soul: str | None = None


class ModelsProbeRequest(BaseModel):
    api_base: str = ""
    api_key: str = ""
    # Only used by the connection test (a real completion needs a model);
    # the model-list probe ignores it. Blank falls back to the saved config.
    model: str = ""


class SettingsUpdateRequest(BaseModel):
    updates: dict[str, Any] = {}
    # Cascade layer the write lands in (ADR 0047 slice 3): "agent" (the leaf, default)
    # or "host" (the box-shared host-config.yaml, host-scoped non-secret keys only).
    layer: str = "agent"


class SettingsResetRequest(BaseModel):
    keys: list[str] = []


def _reset_live_embed_breaker() -> None:
    """Clear the live knowledge store's embedding circuit breaker, if it has one.

    Duck-typed: only ``HybridKnowledgeStore`` exposes ``reset_embed_breaker``; a
    keyword-only base store or a plugin backend simply has no breaker to reset."""
    store = STATE.knowledge_store
    reset = getattr(store, "reset_embed_breaker", None)
    if callable(reset):
        try:
            if reset():
                import logging

                logging.getLogger("protoagent.server").info(
                    "[knowledge] embedding breaker cleared (live key tested OK)"
                )
        except Exception:  # noqa: BLE001 — never let a breaker reset break the test route
            pass


def register_config_routes(app) -> None:
    """Register the ``/api/config*`` + ``/api/settings*`` routes on ``app``."""

    # --- Live config / SOUL editing ----------------------------------------
    # GET returns the current config + persona so external clients (the console
    # Settings drawer is one; curl is another) can mirror what's running.
    # POST accepts partial edits — pass only the sections you want to
    # change. Reload is automatic.
    @app.get("/api/config")
    async def _api_get_config():
        from graph.config_io import config_to_dict, read_soul

        return {
            "config": config_to_dict(STATE.graph_config),
            "soul": read_soul(),
        }

    @app.get("/api/config/explain")
    async def _api_config_explain():
        """Read-only diagnostic: this instance's identity, both roots, every
        resolved path, and the per-field cascade provenance — the console/curl
        counterpart of ``python -m server config explain``. Passes the LIVE
        config so the cascade reflects what's actually running; secrets are
        redacted to a set/unset marker, never echoed."""
        from graph.config_explain import build_config_explain

        return build_config_explain(STATE.graph_config)

    @app.post("/api/config")
    async def _api_post_config(req: ConfigReloadRequest):
        # Offload off the event loop (#497) — the reload's graph compile is heavy
        # and would otherwise freeze the server for its duration.
        ok, messages = await asyncio.to_thread(_apply_settings_changes, config=req.config, soul=req.soul)
        return {"ok": ok, "messages": messages}

    @app.post("/api/config/models")
    async def _api_list_models(req: ModelsProbeRequest | None = None):
        """Fetch the gateway's model list.

        POST (body) not GET (query) so the caller's API key doesn't
        end up in browser history, reverse-proxy access logs, or the
        uvicorn request log. A blank body falls back to whatever key
        and base are stored in the current config — useful for the
        drawer's initial render where there's nothing to POST yet.
        """
        from graph.config_io import list_gateway_models

        body = req or ModelsProbeRequest()
        base = body.api_base or (STATE.graph_config.api_base if STATE.graph_config else "")
        key = body.api_key or (STATE.graph_config.api_key if STATE.graph_config else "")
        models, error = list_gateway_models(base, key)
        return {"models": models, "error": error}

    @app.post("/api/config/test-model")
    async def _api_test_model(req: ModelsProbeRequest | None = None):
        """Verify the model can actually complete (the true auth check).

        Powers the wizard's + Settings' "Test connection" button. POST (body)
        so the key never lands in a URL/log. A blank field falls back to the
        saved config, so Settings can re-test the live agent with one click.
        Offloaded to a thread — a real completion is a blocking network call,
        and we never want the connection test to freeze the event loop.
        """
        from graph.config_io import validate_model_connection

        body = req or ModelsProbeRequest()
        base = body.api_base or (STATE.graph_config.api_base if STATE.graph_config else "")
        key = body.api_key or (STATE.graph_config.api_key if STATE.graph_config else "")
        model = body.model or (STATE.graph_config.model_name if STATE.graph_config else "")
        ok, error = await asyncio.to_thread(validate_model_connection, base, key, model)
        # A successful test of the LIVE saved key (no form-local override) proves the
        # gateway + key are good again — so clear any open embedding circuit breaker
        # now, instead of waiting out the cooldown. This is the recovery path for an
        # out-of-band key fix (hand-edited secrets.yaml, env var, gateway-side fix):
        # the settings-save path already resets it by rebuilding the store. Embeds use
        # the same api_base/key, so a chat-completion success is a sound signal.
        if ok and not body.api_key:
            _reset_live_embed_breaker()
        return {"ok": ok, "error": error}

    # `/api/config/test-discord` (discord plugin) and `/api/config/google/status`
    # + `/connect` (google plugin) are now mounted by their plugin routers (ADR
    # 0018/0019), at the same paths — the console Test/Connect buttons are
    # unchanged.

    # --- Setup wizard state -------------------------------------------------
    @app.get("/api/config/setup-status")
    async def _api_setup_status():
        from graph.config_io import is_setup_complete, list_soul_presets

        return {
            "setup_complete": is_setup_complete(),
            "presets": list_soul_presets(),
        }

    @app.post("/api/config/setup")
    async def _api_finish_setup(req: ConfigReloadRequest):
        """Terminal wizard action over HTTP. Same semantics as the
        drawer's ``finish_setup`` callback — writes everything, marks
        setup complete, optionally installs autostart, then reloads.
        """
        callbacks = _build_settings_callbacks()
        # Offload off the event loop (#497) — finish-setup validates the model +
        # compiles the graph, both heavy; running inline froze the server ~30s.
        ok, msg = await asyncio.to_thread(callbacks["finish_setup"], req.config, req.soul)
        return {"ok": ok, "message": msg}

    @app.post("/api/config/reset-setup")
    async def _api_reset_setup():
        from graph.config_io import reset_setup

        reset_setup()
        return {"ok": True, "message": "setup marker removed"}

    @app.get("/api/config/presets/{name}")
    async def _api_read_preset(name: str):
        from graph.config_io import read_soul_preset

        return {"name": name, "content": read_soul_preset(name)}

    @app.get("/api/acp-agents")
    async def _api_acp_agents():
        """The canonical ACP coding-agent catalog (id, label, command, args) — one source
        for the Delegates picker, the setup wizard, and the agent_runtime options."""
        from runtime.acp_agents import acp_agent_catalog

        return {"agents": acp_agent_catalog()}

    # --- Generic settings (schema-driven UI) --------------------------------
    @app.get("/api/settings/schema")
    async def _api_settings_schema():
        """All editable settings, grouped, with current values + metadata
        (type, default, restart-vs-hot-reload, description). Drives the
        operator console's Settings surface."""
        from graph.config import _load_host_layer
        from graph.config_io import config_yaml_path, list_gateway_models, load_yaml_doc
        from graph.settings_schema import build_schema

        models: list[str] = []
        if STATE.graph_config is not None:
            models, _ = list_gateway_models(STATE.graph_config.api_base, STATE.graph_config.api_key)
        # Per-layer provenance (ADR 0047): the raw agent leaf doc + the filtered Host
        # layer let build_schema report each field's `source` (agent/host/default) so
        # the UI can badge inherited-vs-overridden.
        _cfg_yaml = config_yaml_path()
        agent_doc = load_yaml_doc(_cfg_yaml) if _cfg_yaml.exists() else {}
        host_doc = _load_host_layer()
        return {
            "groups": build_schema(
                STATE.graph_config,
                model_options=models,
                agent_doc=agent_doc if isinstance(agent_doc, dict) else {},
                host_doc=host_doc,
            )
        }

    @app.post("/api/settings")
    async def _api_save_settings(req: SettingsUpdateRequest):
        """Validate a flat {key: value} payload, persist it to the chosen cascade
        layer (``layer``: "agent" leaf, default, or "host" box-shared file; secrets
        split out / refused on host), and hot-reload the graph. Returns any keys that
        need a full process restart to take effect."""
        from graph.settings_schema import nest_updates, restart_keys, validate_flat

        ok, err = validate_flat(req.updates)
        if not ok:
            return {"ok": False, "messages": [f"validation: {err}"], "restart_required": []}
        # Offload off the event loop (#497) — a model change recompiles the graph.
        ok, messages = await asyncio.to_thread(
            _apply_settings_changes, config=nest_updates(req.updates), layer=req.layer
        )
        return {"ok": ok, "messages": messages, "restart_required": restart_keys(req.updates)}

    @app.post("/api/settings/reset")
    async def _api_reset_settings(req: SettingsResetRequest):
        """Reset-to-inherited (ADR 0047 slice 3): pop the given dotted keys from the
        agent leaf YAML + reload, so each falls back to the Host/App layer. Only
        known settings keys are accepted (existence-gated against the registry —
        a reset carries no value, so the per-type validate_flat checks don't apply)."""
        from graph.settings_schema import is_known_key

        for k in req.keys:
            if not is_known_key(k):
                return {"ok": False, "messages": [f"validation: unknown setting: {k}"]}
        ok, messages = await asyncio.to_thread(_reset_settings_keys, req.keys)
        return {"ok": ok, "messages": messages}
