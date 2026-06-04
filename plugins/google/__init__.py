"""Google (Gmail + Calendar) surface as a plugin (ADR 0017 → 0019).

The integration is a **managed MCP server** (``mcp_servers/google/``) the agent
connects to; this plugin is the *wiring* that lives in a plugin instead of
``server.py`` + ``tools/mcp_tools.py``. Moving it here lets a fork disable Google
(``plugins.disabled: [google]``) or replace it with its own integration, with no
core edit.

Contributes:
- a **managed MCP server** (``register_mcp_server``) — OAuth-gated, frozen-aware
  launch, started only once connected (a cached token exists);
- two **routes** (``GET /api/config/google/status`` + ``POST
  /api/config/google/connect``) mounted at their existing paths so the console's
  Connect/Status UI is unchanged;
- a frozen-binary entrypoint (``mcp_main``) the ``--mcp-plugin google`` shim runs.

Config/secrets/Settings come from the manifest (ADR 0019); it claims the existing
top-level ``google`` section, so saved OAuth clients keep working.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("protoagent.plugins.google")


def mcp_main() -> None:
    """Frozen-binary entrypoint (``--mcp-plugin google``) — run the managed
    Google MCP server in this process. The desktop binary has no ``python`` on
    PATH, so the server factory re-invokes the binary with ``--mcp-plugin
    google`` and the shim calls this."""
    from mcp_servers.google.server import main as _google_mcp_main

    _google_mcp_main()


def _gcfg(config) -> dict:
    """The resolved ``google`` plugin-config section from a live LangGraphConfig."""
    return (getattr(config, "plugin_config", {}) or {}).get("google", {}) or {}


def _token_path() -> str:
    try:
        from graph.config_io import _live_config_dir

        return str(_live_config_dir() / "google-token.json")
    except Exception:  # noqa: BLE001
        return "google-token.json"


def _server_factory(config) -> dict | None:
    """Build the managed Google MCP server entry, or None if google is off / not
    yet connected. Called at every graph build with the live config (ADR 0019).

    The OAuth client (id/secret) + token path + tz go to the subprocess via
    ``env``. Launch is frozen-aware: the bundled binary has no ``python`` on PATH,
    so it re-invokes itself (``<binary> --mcp-plugin google``); a normal
    interpreter runs ``-m mcp_servers.google.server``.
    """
    g = _gcfg(config)
    if not g.get("enabled"):
        return None
    client_id = (g.get("client_id") or "").strip()
    client_secret = (g.get("client_secret") or "").strip()
    if not (client_id and client_secret):
        log.info("[google] enabled but OAuth client not set — server not started")
        return None

    # Start the headless MCP server only once authorized (a cached token exists).
    # Before "Connect Google" there's nothing to load — it would just register
    # tools that error on every call. The connect route reloads once the token is
    # written, so the server comes up then.
    token_path = _token_path()
    if not os.path.exists(token_path):
        log.info("[google] enabled but not connected yet — server starts after Connect Google")
        return None

    import sys

    if getattr(sys, "frozen", False):
        command, args = sys.executable, ["--mcp-plugin", "google"]
    else:
        command, args = sys.executable, ["-m", "mcp_servers.google.server"]

    env = {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
        "GOOGLE_TOKEN_PATH": token_path,
    }
    tz = (g.get("tz") or "").strip()
    if tz:
        env["GOOGLE_TZ"] = tz

    return {
        "name": "google",
        "enabled": True,
        "transport": "stdio",
        "command": command,
        "args": args,
        "env": env,
    }


def _env_from_config(config) -> None:
    """Mirror the configured OAuth client + token path into the env so
    ``mcp_servers.google.auth`` (which reads env) can run consent/status
    in-process for the connect + status routes."""
    g = _gcfg(config)
    cid = (g.get("client_id") or "").strip()
    sec = (g.get("client_secret") or "").strip()
    if cid:
        os.environ["GOOGLE_CLIENT_ID"] = cid
    if sec:
        os.environ["GOOGLE_CLIENT_SECRET"] = sec
    os.environ["GOOGLE_TOKEN_PATH"] = _token_path()


def _build_router(host):
    """The Connect/Status routes, mounted at their existing paths (prefix="")."""
    import asyncio

    from fastapi import APIRouter

    router = APIRouter()

    def _live_config():
        return host.config() if host and host.config else None

    @router.get("/api/config/google/status")
    async def _google_status():
        """Report (configured, connected, email) for the Google surface."""
        try:
            from mcp_servers.google.auth import connection_status
        except Exception as e:  # noqa: BLE001 — google extra may be absent
            return {"configured": False, "connected": False, "email": None,
                    "error": f"google support unavailable: {e}"}
        _env_from_config(_live_config())
        try:
            return await asyncio.to_thread(connection_status)
        except Exception as e:  # noqa: BLE001
            return {"configured": False, "connected": False, "email": None, "error": str(e)}

    @router.post("/api/config/google/connect")
    async def _google_connect():
        """Run the OAuth consent (opens the operator's browser), cache the token,
        enable the Google surface, and reload so the tools register. Long-lived:
        it blocks until the operator approves in the browser (3-min cap)."""
        try:
            from mcp_servers.google.auth import run_consent
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"google support unavailable: {e}"}
        cfg = _live_config()
        g = _gcfg(cfg)
        if not ((g.get("client_id") or "").strip() and (g.get("client_secret") or "").strip()):
            return {"ok": False, "error": "Set the OAuth client ID + secret first, then connect."}
        _env_from_config(cfg)
        try:
            email = await asyncio.wait_for(asyncio.to_thread(run_consent), timeout=180)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Timed out waiting for Google consent (3 min). Try again."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        # Persist enabled + reload so the managed google MCP server starts and the
        # tools register without a restart.
        msg = None
        if host and host.apply_settings:
            ok, msg = await asyncio.to_thread(host.apply_settings, {"google": {"enabled": True}})
            msg = msg if ok else None
        return {"ok": True, "email": email, "reload": msg}

    return router


def register(registry) -> None:
    registry.register_mcp_server(_server_factory)
    registry.register_router(_build_router(registry.host), prefix="")  # existing /api paths
