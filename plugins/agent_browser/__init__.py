"""agent_browser — browser automation for protoAgent, backed by vercel-labs/agent-browser.

Composition over construction: this plugin is a thin shell over the `agent-browser`
CLI/daemon. It contributes the browser **tools** (subprocess wrappers), a discovery
**skill** + browser **workflows** (auto-discovered from skills/ and workflows/), and an
interactive **Browser panel** console view — a live, drivable CDP-screencast viewport
(browser_stream bridges Chrome's CDP to a canvas over a gated WebSocket). It does NOT
reimplement browser automation or a renderer.

Bundled in-tree (#3451), superseding the standalone ``agent-browser-plugin`` repo. Ships
DISABLED. Enable with `plugins: { enabled: [agent_browser] }` and put the `agent-browser`
binary on PATH (`npm i -g agent-browser && agent-browser install`) — if it isn't there,
``preflight`` reports it as an operator setup-gap banner instead of letting every browser
call fail silently into the model's loop.

**The binary is PATH-only, deliberately.** A managed, pinned+checksummed download (the
``infra/br_fetch.py`` shape) is a separate slice behind an ADR: it has to answer whether
fetching a third-party native binary at runtime falls under the no-bundled-``br`` order,
and that is the operator's call, not this plugin's.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.agent_browser")


def register(registry) -> None:
    cfg = registry.config or {}

    # Preflight FIRST: an operator whose CLI is missing sees a banner the moment the
    # plugin loads, rather than discovering it through a failed tool call. Reports two
    # gaps — the CLI on PATH, and whether that CLI has a Chrome to drive.
    from . import preflight
    try:
        probe = preflight.report(registry, cfg)
        log.info("[agent_browser] preflight: cli=%s version=%r chrome=%s",
                 probe.cli_path or "MISSING", probe.cli_version, probe.chrome)
    except Exception:  # noqa: BLE001 — a preflight must never break loading
        log.exception("[agent_browser] preflight failed")

    def _refresh_gaps() -> None:
        """Re-probe and re-report — called by the tools when a run finds the CLI missing,
        and again on the first success afterwards, so the banner self-heals live."""
        preflight.report(registry, registry.live_config() or cfg)

    # Browser tools (subprocess wrappers over the agent-browser CLI).
    try:
        from .tools import get_browser_tools
        for t in get_browser_tools(cfg, refresh_gaps=_refresh_gaps):
            registry.register_tool(t)
    except Exception:  # noqa: BLE001 — tools are the foundation; log loudly if they fail
        log.exception("[agent_browser] registering browser tools failed")

    # Interactive Browser panel console view. Register it best-effort so the tools still
    # serve if the panel can't import. TWO routers at DISTINCT prefixes: the PAGE stays on
    # the public /plugins/agent_browser (an iframe page-load can't carry a bearer); the
    # DATA routes (the nav toolbar, the stream ticket + the /stream WS) mount under
    # /api/plugins/agent_browser so the HTTP ones inherit the operator bearer gate
    # (plugin-view rule 2). The WS gates itself with a single-use ticket — the host's auth
    # middleware doesn't cover WS handshakes.
    try:
        from .browser_panel import build_panel_data_router, build_panel_router
        registry.register_router(build_panel_router(cfg))
        registry.register_router(build_panel_data_router(cfg), prefix="/api/plugins/agent_browser")
    except ImportError:
        log.info("[agent_browser] browser panel not present yet — tools still serve")
    except Exception:  # noqa: BLE001
        log.exception("[agent_browser] mounting browser panel failed")

    # skills/ and workflows/ are auto-discovered (ADR 0027) — no register call.
    log.info("[agent_browser] registered browser tools (binary=%s)", cfg.get("binary", "agent-browser"))
