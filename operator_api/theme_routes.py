"""Per-agent theme persistence (ADR 0042 / fleet).

Each agent (workspace) saves its **own** theme, so the in-place switch repaints the
console to the focused agent's look — the theme is just another per-``PROTOAGENT_CONFIG_DIR``
setting, and the proxy routes ``/agents/<slug>/api/theme`` to that agent (ADR 0042 slug routing).

Storage is **opaque**: the front-end's ThemePanel owns the token schema (@protolabsai/ui);
the server just persists the blob in ``<config_dir>/theme.json`` and hands it back. So new
tokens/formats need no server change.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("protoagent.server")


def _theme_path():
    # Instance-scoped (ADR 0004), same as config/secrets — co-located instances
    # (default + scripts/dev.sh sandbox) must not share one theme.json.
    from graph.config_io import THEME_JSON_PATH

    return THEME_JSON_PATH


def register_theme_routes(app) -> None:
    from fastapi import Body

    @app.get("/api/theme")
    async def _get_theme():
        """This agent's saved theme, or ``null`` (front-end falls back to defaults)."""
        f = _theme_path()
        if not f.exists():
            return {"theme": None}
        try:
            return {"theme": json.loads(f.read_text())}
        except (json.JSONDecodeError, OSError):
            log.warning("[theme] unreadable theme.json at %s", f)
            return {"theme": None}

    @app.put("/api/theme")
    async def _put_theme(body: dict = Body(...)):
        """Persist this agent's theme. Accepts ``{theme: {...}}`` or the raw blob."""
        theme = body.get("theme", body)
        f = _theme_path()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(theme, indent=2) + "\n")
        return {"ok": True}

    @app.delete("/api/theme")
    async def _reset_theme():
        """Clear this agent's theme override (revert to defaults)."""
        f = _theme_path()
        if f.exists():
            f.unlink()
        return {"ok": True}
