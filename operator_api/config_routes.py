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
import logging
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from runtime.state import STATE
from server.agent_init import (
    _apply_settings_changes,
    _build_settings_callbacks,
    _reset_settings_keys,
)

log = logging.getLogger(__name__)


class ConfigReloadRequest(BaseModel):
    config: dict | None = None
    soul: str | None = None


class ModelsProbeRequest(BaseModel):
    api_base: str = ""
    api_key: str = ""
    # Only used by the connection test (a real completion needs a model);
    # the model-list probe ignores it. Blank falls back to the saved config.
    model: str = ""
    # Native OAuth providers (ADR 0097) probe the subscription account instead of
    # the gateway — for "anthropic-oauth"/"openai-codex" the model list + connection
    # test route through the OAuth path, ignoring api_base/api_key. Blank = gateway.
    provider: str = ""


class OAuthLoginRequest(BaseModel):
    """Drives the in-console OAuth sign-in (ADR 0097): `provider` starts a flow,
    `flow_id` (+ `code` for Claude) advances it."""

    provider: str = ""
    flow_id: str = ""
    code: str = ""


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


async def _rebuild_graph_after_reconnect(result: dict, provider: str = "") -> dict:
    """After a completed in-console sign-in, restore the live graph (#2458).

    Two cases warrant the inline reload:

    * The server booted signed-out (``STATE.graph is None``) — the reconnect is
      the moment to rebuild, or the user stays chatless until a manual restart.
    * A live PROVIDER SWITCH failed pre-auth: settings-apply persists the YAML
      first and reloads second, so a switch to a native provider with no
      credential leaves the SAVED provider ≠ the live one and an error with no
      path forward. When the sign-in that just completed is for that saved
      provider, finish the switch now (Josh's live Claude/Codex switches,
      2026-08-10).

    No-op otherwise. Reload failure is reported, not raised — the sign-in
    itself DID succeed and the tokens are stored.
    """
    if result.get("status") != "complete":
        return result
    from graph.config_io import is_setup_complete

    if not is_setup_complete():
        return result
    needs = STATE.graph is None
    if not needs and provider:
        from graph.config import LangGraphConfig
        from graph.config_io import config_yaml_path

        try:
            saved = (LangGraphConfig.from_yaml(config_yaml_path()).model_provider or "").strip().lower()
        except Exception:  # noqa: BLE001 — an unreadable config just means "no pending switch"
            saved = ""
        live = (getattr(STATE.graph_config, "model_provider", "") or "").strip().lower()
        needs = saved == provider and saved != live
    if not needs:
        return result
    from server.agent_init import _reload_langgraph_agent

    ok, message = await asyncio.to_thread(_reload_langgraph_agent)
    if not ok:
        return {**result, "graph_reloaded": False, "graph_reload_error": message}
    return {**result, "graph_reloaded": True}


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

    # --- SOUL.md version history (#1691) -----------------------------------
    # Every persona save archives the outgoing SOUL.md (config_io.write_soul); the console
    # lists these and can roll back to one. Restore re-saves through the same apply path, so
    # it snapshots the CURRENT persona first — rolling back is itself reversible.
    @app.get("/api/config/soul/history")
    async def _api_soul_history():
        from graph.config_io import list_soul_versions

        return {"versions": list_soul_versions()}

    @app.get("/api/config/soul/history/{version_id}")
    async def _api_soul_history_one(version_id: str):
        from graph.config_io import read_soul_version

        content = read_soul_version(version_id)
        if content is None:
            raise HTTPException(status_code=404, detail=f"no SOUL version {version_id!r}")
        return {"id": version_id, "content": content}

    @app.post("/api/config/soul/history/{version_id}/restore")
    async def _api_soul_history_restore(version_id: str):
        from graph.config_io import read_soul, read_soul_version

        content = read_soul_version(version_id)
        if content is None:
            raise HTTPException(status_code=404, detail=f"no SOUL version {version_id!r}")
        # Restoring the version that's already live is a no-op — skip the (expensive) graph
        # recompile a "restore" button on the current row would otherwise trigger.
        if content == read_soul():
            return {"ok": True, "messages": ["already the current persona"], "restored": version_id}
        # Reuse the tested save+reload path: it snapshots the CURRENT persona before overwriting
        # (so restore is reversible) and recompiles the graph off the event loop (#497).
        ok, messages = await asyncio.to_thread(_apply_settings_changes, soul=content)
        return {"ok": ok, "messages": messages, "restored": version_id}

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
        # Native OAuth providers list the subscription account's models, not the gateway's.
        provider = (body.provider or getattr(STATE.graph_config, "model_provider", "") or "").strip().lower()
        from graph.providers import is_native_oauth_provider

        if is_native_oauth_provider(provider):
            from graph.providers.discovery import list_provider_models

            models, error = await asyncio.to_thread(
                list_provider_models, provider, STATE.graph_config
            )
            return {"models": models, "error": error}
        base = body.api_base or (STATE.graph_config.api_base if STATE.graph_config else "")
        key = body.api_key or (STATE.graph_config.api_key if STATE.graph_config else "")
        # Offloaded (#2486): the gateway probe is a blocking network call — running it
        # inline stalled the event loop for the probe duration (the native-OAuth branch
        # above already offloads).
        models, error = await asyncio.to_thread(list_gateway_models, base, key)
        return {"models": models, "error": error}

    @app.get("/api/config/oauth-status")
    async def _api_oauth_status():
        """Sign-in status for the native OAuth providers (ADR 0097) — the wizard +
        Settings render "✓ signed in" / a sign-in hint per provider. Read-only."""
        from graph.providers.discovery import all_oauth_status

        return {"providers": await asyncio.to_thread(all_oauth_status)}

    @app.post("/api/config/oauth/start")
    async def _api_oauth_start(req: OAuthLoginRequest):
        """Begin an in-console OAuth sign-in (ADR 0097). Codex returns a device code +
        URL to poll; Claude returns an authorize URL to open + complete with a code."""
        from graph.providers.oauth_login import OAuthLoginError, login_start

        try:
            return await asyncio.to_thread(login_start, req.provider)
        except OAuthLoginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/config/oauth/poll")
    async def _api_oauth_poll(req: OAuthLoginRequest):
        """Poll a Codex device sign-in — returns {status: pending|complete|error}. On
        completion the tokens are stored and oauth-status flips to signed in."""
        from graph.providers.oauth_login import OAuthLoginError, codex_login_poll

        try:
            result = await asyncio.to_thread(codex_login_poll, req.flow_id)
        except OAuthLoginError as exc:
            return {"status": "error", "error": str(exc)}
        return await _rebuild_graph_after_reconnect(result, "openai-codex")

    @app.post("/api/config/oauth/complete")
    async def _api_oauth_complete(req: OAuthLoginRequest):
        """Complete a Claude sign-in with the pasted `code#state` — exchanges + stores."""
        from graph.providers.oauth_login import OAuthLoginError, anthropic_login_complete

        try:
            result = await asyncio.to_thread(anthropic_login_complete, req.flow_id, req.code)
        except OAuthLoginError as exc:
            return {"status": "error", "error": str(exc)}
        return await _rebuild_graph_after_reconnect(result, "anthropic-oauth")

    @app.post("/api/config/oauth/cancel")
    async def _api_oauth_cancel(req: OAuthLoginRequest):
        """Abandon an in-progress sign-in (#2440) — drops the server-side pending flow so a
        Cancel in the wizard actually cancels the flow, not just the browser timer."""
        from graph.providers.oauth_login import cancel_login

        return await asyncio.to_thread(cancel_login, req.flow_id)

    @app.post("/api/config/oauth/disconnect")
    async def _api_oauth_disconnect(req: OAuthLoginRequest):
        """Disconnect a native OAuth provider (#2440): best-effort remote revoke + delete
        protoAgent's own stored credential + suppress auto-reconnect until an in-console
        sign-in. Never touches the vendor CLI's auth file. Idempotent.

        Disconnect is an application-level auth transition, not just a storage
        mutation (#2459): when the live graph runs on this provider, its in-memory
        client still holds the just-revoked token — leaving it loaded lets the next
        prompt reach the provider and surface a raw 401. Unload it into the same
        signed-out state a disconnected boot produces (#2458); the completed
        sign-in reload restores it.
        """
        from graph.providers.oauth import OAuthCredentialError, disconnect

        if not (req.provider or "").strip():
            raise HTTPException(status_code=400, detail="provider is required")
        try:
            result = await asyncio.to_thread(disconnect, req.provider)
        except OAuthCredentialError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        provider = (req.provider or "").strip().lower()
        live_provider = (getattr(STATE.graph_config, "model_provider", "") or "").strip().lower()
        if STATE.graph is None or provider != live_provider:
            return result.as_dict()
        # TOCTOU guard (QA review): a sign-in completing during the disconnect
        # thread's await rebuilds the graph AND clears the disconnect marker —
        # its work must win. The marker is the storage truth for who finished
        # last, so re-check it after the await; the mutations below then run
        # synchronously (no awaits) so nothing on the loop can interleave.
        from graph.providers.oauth import is_disconnected

        if not is_disconnected(provider):
            return {**result.as_dict(), "graph_unloaded": False}

        warmer, STATE.cache_warmer = STATE.cache_warmer, None
        STATE.graph = None
        STATE.graph_auth_error = {
            "provider": provider,
            "message": f"{provider} is disconnected in protoAgent. Sign in again to reconnect.",
            "relogin": True,
        }
        # The cache warmer pings this provider on a heartbeat — stopped AFTER the
        # unload commit (its await is the yield point the guard above protects),
        # detached from the just-committed signed-out state.
        if warmer is not None:
            try:
                await warmer.stop()
            except Exception:  # noqa: BLE001 — a warmer that won't stop must not block disconnect
                log.warning("[oauth] cache warmer stop failed during disconnect", exc_info=True)
        return {**result.as_dict(), "graph_unloaded": True}

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
        model = body.model or (STATE.graph_config.model_name if STATE.graph_config else "")
        # Native OAuth providers (ADR 0097) test through the subscription, not a gateway
        # key — build the real client and stream a 1-token turn.
        provider = (body.provider or getattr(STATE.graph_config, "model_provider", "") or "").strip().lower()
        from graph.providers import is_native_oauth_provider

        if is_native_oauth_provider(provider):
            from graph.providers.discovery import validate_oauth_connection

            ok, error = await asyncio.to_thread(
                validate_oauth_connection, provider, model, STATE.graph_config
            )
            return {"ok": ok, "error": error}
        base = body.api_base or (STATE.graph_config.api_base if STATE.graph_config else "")
        key = body.api_key or (STATE.graph_config.api_key if STATE.graph_config else "")
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

        # Offloaded (#2486) — filesystem work stays off the event loop like its siblings.
        await asyncio.to_thread(reset_setup)
        return {"ok": True, "message": "setup marker removed"}

    @app.get("/api/config/presets/{name}")
    async def _api_read_preset(name: str):
        from graph.config_io import list_soul_presets, read_soul_preset

        # An UNKNOWN name is a 404 (#2486) — the old empty-string 200 was
        # indistinguishable from a real-but-blank preset. Membership decides, not
        # content: read_soul_preset's ""-for-unknown contract stays for the wizard's
        # internal blank-canvas path, and a listed preset that happens to be empty
        # still 200s.
        if name not in await asyncio.to_thread(list_soul_presets):
            raise HTTPException(status_code=404, detail=f"no soul preset {name!r}")
        return {"name": name, "content": await asyncio.to_thread(read_soul_preset, name)}

    @app.get("/api/projects")
    async def _api_projects():
        """The ADR 0095 managed-projects registry — READ-ONLY (D5 slice 1).

        Registration is still a YAML edit; this surfaces what's registered and, more
        importantly, **whether the registry is actually in effect**. An explicit
        ``filesystem.projects`` wins over the registry by design (that's what makes
        0095 non-regressing), which means a fully-populated registry can be sitting
        there driving nothing. Silent divergence between what's declared and what's
        enforced is the exact failure 0095 exists to kill, so it's reported rather
        than left for the operator to infer:

        - ``fence_source`` — who is actually feeding the fs fence right now:
          ``explicit`` (``filesystem.projects`` is set and SHADOWS the registry) ·
          ``registry`` · ``workspace_default`` · ``disabled``.
        - per-row ``fenced`` — whether THAT entry contributes to the live fence
          (false when it opts out with ``fs: false``, or when explicit roots shadow
          the whole registry).
        - per-row ``exists`` — same liveness check the work-folder editor reports; a
          path that isn't a directory is dropped at graph build (#2251).

        No POST: the work-folder editor's write path REPLACES ``filesystem.projects``,
        so a naive write-back here would silently materialize the projection into
        explicit entries and sever the registry link. Editing stays YAML-only until
        that has a real answer."""
        from pathlib import Path

        cfg = STATE.graph_config
        registry = list(getattr(cfg, "projects", []) or [])
        explicit = list(getattr(cfg, "filesystem_projects", []) or [])
        enabled = bool(getattr(cfg, "filesystem_enabled", False))
        # The PROJECTION, in order — not a set. fenced_projects() already drops the
        # malformed and duplicate entries (keeping the first of a duplicate name), so
        # matching a raw registry row by name against a set would mark BOTH halves of a
        # duplicate as fenced when only one root is reachable.
        fenced_rows = cfg.fenced_projects() if hasattr(cfg, "fenced_projects") else []
        # Keyed on (name, PATH), not name alone. fenced_projects() resolves duplicates by
        # keeping the first entry — which may be a different PATH than a later row sharing
        # the name. Matching on name would then mark the wrong row as fenced: with
        # [{dup, /missing}, {dup, /exists}] the fence holds /missing (dropped downstream as
        # a non-directory, so the real fence is EMPTY) while the API would report the
        # /exists row as live. That is the declared-vs-enforced divergence this endpoint
        # exists to expose, so it must not be reintroduced here.
        fenced_keys = {(str(p.get("name") or ""), str(p.get("path") or "")) for p in fenced_rows}

        if not enabled:
            fence_source = "disabled"
        elif explicit:
            fence_source = "explicit"
        elif registry:
            # A CONFIGURED registry is the fence source even when it projects nothing —
            # that's the whole point of honouring `fs: false` literally rather than
            # substituting the workspace default. Refined to "unbound" below when no
            # row actually feeds the fence.
            fence_source = "registry"
        else:
            fence_source = "workspace_default"

        projects = []
        claimed: set[tuple[str, str]] = set()
        for entry in registry:
            row = dict(entry) if isinstance(entry, dict) else {}
            raw = str(row.get("path") or "").strip()
            try:
                row["exists"] = bool(raw) and Path(raw).expanduser().is_dir()
            except OSError:  # unreadable mount / bad path — same as gone for our purposes
                row["exists"] = False
            # `exists` is part of "feeds the live fence", not a separate nicety:
            # fenced_projects() is a pure CONFIG projection, while the real fence
            # (tools/fs_tools.py::_registry_from_config) drops any root whose path
            # isn't a directory. So a registered-but-missing folder contributes
            # nothing, and reporting it as fenced would be the same declared-vs-
            # enforced lie this endpoint exists to expose.
            name = str(row.get("name") or "")
            # Compare the EXPANDED path, since fenced_projects() expands `~` — a row
            # written as `~/dev/x` must still match the fence entry it produced.
            try:
                # Resolved, in lockstep with fenced_projects() — it emits resolved paths,
                # so comparing an unresolved one here would never match a symlinked entry.
                key = (name, str(Path(raw).expanduser().resolve())) if raw else (name, "")
            except (OSError, ValueError):
                key = (name, raw)
            row["fenced"] = bool(
                fence_source == "registry"
                and row["exists"]
                and key in fenced_keys
                and key not in claimed  # the SECOND row of a duplicate isn't the fenced one
            )
            if row["fenced"]:
                claimed.add(key)
            projects.append(row)

        # A configured registry where NO row feeds the fence resolves to an empty fence,
        # and build_fs_tools then unbinds the entire toolset (#2251). Reporting
        # "registry" there would claim the registry is driving a fence that doesn't
        # exist. Two distinct ways to land here, both worth naming rather than hiding:
        # every entry opted out with `fs: false` (deliberate), or every folder is
        # missing (a mistake). The console tells them apart from the rows.
        if fence_source == "registry" and not any(p.get("fenced") for p in projects):
            fence_source = "unbound"

        return {
            "enabled": enabled,
            "fence_source": fence_source,
            "projects": projects,
        }

    @app.get("/api/settings/filesystem-projects")
    async def _api_fs_projects():
        """The fenced fs roots (ADR 0007): the explicit ``filesystem.projects`` list
        plus whether filesystem tools are enabled. Backs the Tools-panel folder
        editor — previously this collection had no UI or API at all (YAML-only).

        Each row carries ``exists``: ``build_fs_tools`` silently SKIPS a root whose
        path is no longer a directory, and when that empties the registry the whole
        fs toolset disappears with only a log line. Reporting liveness here
        lets the editor say which folder went missing instead of the operator having
        to diff the tool list against the YAML."""
        from pathlib import Path

        cfg = STATE.graph_config
        projects = []
        for entry in list(getattr(cfg, "filesystem_projects", []) or []):
            row = dict(entry) if isinstance(entry, dict) else {}
            raw = str(row.get("path") or "").strip()
            try:
                row["exists"] = bool(raw) and Path(raw).expanduser().is_dir()
            except OSError:  # unreadable mount / bad path — same as gone for our purposes
                row["exists"] = False
            projects.append(row)
        return {
            "enabled": bool(getattr(cfg, "filesystem_enabled", False)),
            "projects": projects,
        }

    @app.post("/api/settings/filesystem-projects")
    async def _api_fs_projects_set(body: dict | None = None):
        """Replace ``filesystem.projects`` (same replace-list pattern as
        POST /api/mcp/servers): each entry ``{name?, path, write?}`` — name defaults
        from the folder basename, paths are ~-expanded, blanks and duplicate names
        are rejected. An empty list clears explicit roots (fs falls back to the
        workspace default when enabled).

        Every path must be an ABSOLUTE, EXISTING directory. ``build_fs_tools`` drops
        roots that don't resolve to a directory, and once every configured root is
        dropped the registry is empty and the entire fs toolset unbinds — so saving
        one unusable folder here used to silently take read_file/list_dir/write_file
        away from the agent, with nothing but a log warning to say why. A
        relative path is refused for the same reason: it would resolve against the
        server's CWD (``/`` under the desktop sidecar), never the operator's."""
        from pathlib import Path

        raw = (body or {}).get("projects")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="projects must be a list")
        projects: list[dict] = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                raise HTTPException(status_code=400, detail="each project must be an object")
            path = str(entry.get("path", "")).strip()
            if not path:
                raise HTTPException(status_code=400, detail="each project needs a path")
            expanded = Path(path).expanduser()
            if not expanded.is_absolute():
                raise HTTPException(
                    status_code=400,
                    detail=f"folder path must be absolute (start with / or ~): {path!r}",
                )
            try:
                is_dir = expanded.is_dir()
            except OSError as exc:
                raise HTTPException(status_code=400, detail=f"can't read folder {expanded}: {exc}") from exc
            if not is_dir:
                raise HTTPException(
                    status_code=400,
                    detail=f"no such folder: {expanded} — create it first, or fix the path",
                )
            name = str(entry.get("name", "")).strip() or expanded.name or "folder"
            if name in seen:
                raise HTTPException(status_code=400, detail=f"duplicate project name: {name}")
            seen.add(name)
            projects.append({"name": name, "path": str(expanded), "write": bool(entry.get("write", False))})

        from server.agent_init import _apply_settings_changes

        updates: dict = {"filesystem": {"projects": projects}}
        if projects and (body or {}).get("enable", True):
            updates["filesystem"]["enabled"] = True
        # Offloaded like every sibling call site (#497): config-YAML write + full graph
        # reload must not run on the event loop. This was the one site that didn't (#2210).
        ok, messages = await asyncio.to_thread(_apply_settings_changes, config=updates)
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "projects": projects}

    @app.get("/api/acp-agents")
    async def _api_acp_agents():
        """The canonical ACP coding-agent catalog (id, label, command, args) — one source
        for the Delegates picker, the setup wizard, and the agent_runtime options. Includes
        any user-registered ``acp.agents.<id>`` (custom agents / launch-spec overrides)."""
        from runtime.acp_agents import acp_agent_catalog

        extra = getattr(STATE.graph_config, "acp_agents", None) if STATE.graph_config else None
        return {"agents": acp_agent_catalog(extra)}

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
            # Native OAuth providers (ADR 0097) list the subscription's models, not the
            # gateway's — same branch as POST /api/config/models. Without it the schema's
            # model.name options collapsed to the configured model, so /model and the
            # composer picker offered exactly one card while Settings ▸ Get models saw
            # nine (#2473). Offloaded: both listers are blocking network probes.
            provider = (getattr(STATE.graph_config, "model_provider", "") or "").strip().lower()
            from graph.providers import is_native_oauth_provider

            if is_native_oauth_provider(provider):
                from graph.providers.discovery import list_provider_models

                models, _ = await asyncio.to_thread(list_provider_models, provider, STATE.graph_config)
            else:
                models, _ = await asyncio.to_thread(
                    list_gateway_models, STATE.graph_config.api_base, STATE.graph_config.api_key
                )
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

        # settings.hidden (#2172): hidden keys are absent from the schema AND refused
        # here — the UI is presentation, this is the write-side half of the lock.
        hidden = getattr(STATE.graph_config, "settings_hidden", None) or []
        ok, err = validate_flat(req.updates, hidden=hidden)
        if not ok:
            return {"ok": False, "messages": [f"validation: {err}"], "restart_required": []}
        # Validation + restart-key reporting are this surface's; the apply itself goes through
        # the shared config.set op (ADR 0075 D2). The op offloads the graph recompile off the
        # event loop (#497); the live applier is the same _apply_settings_changes as before.
        from ops import OpContext
        from ops.config import set_config

        result = await set_config(
            nest_updates(req.updates),
            ctx=OpContext.from_state(),
            apply_settings=lambda u: _apply_settings_changes(config=u, layer=req.layer),
        )
        return {"ok": result.ok, "messages": result.messages, "restart_required": restart_keys(req.updates)}

    @app.post("/api/settings/reset")
    async def _api_reset_settings(req: SettingsResetRequest):
        """Reset-to-inherited (ADR 0047 slice 3): pop the given dotted keys from the
        agent leaf YAML + reload, so each falls back to the Host/App layer. Only
        known settings keys are accepted (existence-gated against the registry —
        a reset carries no value, so the per-type validate_flat checks don't apply)."""
        from graph.settings_schema import is_hidden_setting, is_known_key

        hidden = getattr(STATE.graph_config, "settings_hidden", None) or []
        for k in req.keys:
            if not is_known_key(k):
                return {"ok": False, "messages": [f"validation: unknown setting: {k}"]}
            # settings.hidden (#2172): a reset writes too (pops the leaf → the value
            # falls back to Host/App), so a hidden key is just as locked here.
            if is_hidden_setting(k, hidden):
                return {"ok": False, "messages": [f"validation: {k} is locked by settings.hidden"]}
        ok, messages = await asyncio.to_thread(_reset_settings_keys, req.keys)
        return {"ok": ok, "messages": messages}
