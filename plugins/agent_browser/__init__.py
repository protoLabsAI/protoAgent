"""agent_browser — browser automation for protoAgent, backed by vercel-labs/agent-browser.

Composition over construction: this plugin is a thin shell over the `agent-browser`
CLI/daemon. It contributes the browser **tools** (subprocess wrappers), a discovery
**skill** + browser **workflows** (auto-discovered from skills/ and workflows/), and an
interactive **Browser panel** console view — a live, drivable CDP-screencast viewport
(browser_stream bridges Chrome's CDP to a canvas over a gated WebSocket). It does NOT
reimplement browser automation or a renderer.

Bundled in-tree (#3451), superseding the standalone ``agent-browser-plugin`` repo. Ships
DISABLED. Enable with `plugins: { enabled: [agent_browser] }`.

**The CLI installs itself.** When no `agent-browser` is on PATH, the plugin downloads the
PINNED, sha256-verified upstream release on first use (``cli_fetch`` — the ``br_fetch``
shape; the operator's 2026-09-12 ruling: a download, not a bundled binary), or when the
operator clicks the setup banner's **Download agent-browser** button. The browser it drives
is installed only by the banner's **Install Chrome** button (``chrome_install`` runs the
CLI's own `agent-browser install`) — never from a tool call. A PATH install (`npm i -g
agent-browser`) and a path pinned in `binary` always win over the download. ``preflight``
reports whatever is still missing as an operator setup-gap banner instead of letting every
browser call fail silently into the model's loop.
"""

from __future__ import annotations

import logging

# Module-level, not inside register(): a call-time `from . import x` resolves through
# sys.modules, which a later load of this package (a reload, a second test loader) has
# replaced — register() would then report through ANOTHER copy's preflight and state.
from . import preflight, setup_steps

log = logging.getLogger("protoagent.plugins.agent_browser")


def register(registry) -> None:
    cfg = registry.config or {}

    def _live_cfg() -> dict:
        return registry.live_config() or cfg

    def _refresh_gaps():
        """Re-probe and re-report. Returns the probe, so the tools track what the banner
        actually says rather than what the last call's exit code suggested."""
        return preflight.report(registry, _live_cfg())

    # The setup steps behind the banners' buttons ("Download agent-browser", "Install
    # Chrome"). Registered BEFORE the preflight reports, so the first banner's buttons
    # already work. An older host has no steps — its sanitizer drops the unknown action kind
    # and the banner degrades to its message.
    add_step = getattr(registry, "register_setup_step", None)
    if callable(add_step):
        try:
            add_step(preflight.STEP_DOWNLOAD_CLI, lambda: setup_steps.download_cli(_live_cfg(), _refresh_gaps))
            add_step(preflight.STEP_INSTALL_CHROME, lambda: setup_steps.install_chrome(_live_cfg(), _refresh_gaps))
        except Exception:  # noqa: BLE001 — the buttons are a convenience; the banner still says what to do
            log.exception("[agent_browser] registering the setup steps failed")

    # Preflight FIRST: an operator whose CLI is missing sees a banner the moment the
    # plugin loads, rather than discovering it through a failed tool call. Reports two
    # gaps — the CLI, and whether that CLI has a Chrome to drive. It never downloads.
    start_gap = False
    try:
        probe = preflight.report(registry, cfg)
        # The tools must START in the state the boot probe found: a banner raised here has to
        # be cleared by the first good call even when no call ever fails first (otherwise an
        # operator who fixed the setup before using the browser kept the banner forever).
        start_gap = bool(preflight.hint(probe))
        log.info("[agent_browser] preflight: cli=%s (%s) version=%r chrome=%s %s",
                 probe.cli_path or "MISSING", probe.source or "-", probe.cli_version, probe.chrome,
                 probe.chrome_version)
    except Exception:  # noqa: BLE001 — a preflight must never break loading
        log.exception("[agent_browser] preflight failed")

    # Browser tools (subprocess wrappers over the agent-browser CLI).
    try:
        from .tools import get_browser_tools
        for t in get_browser_tools(cfg, refresh_gaps=_refresh_gaps, start_gap=start_gap):
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
