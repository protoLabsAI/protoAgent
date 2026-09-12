"""The setup steps behind the setup-gap buttons — what **Download agent-browser** and
**Install Chrome** actually do.

``__init__.register`` hands both to ``registry.register_setup_step``; the console's
``plugin_setup`` button POSTs ``/api/plugins/agent_browser/setup-steps/<step>`` and the host
calls the step off the event loop. Each one STARTS its work on a daemon thread, re-reports
the gaps at once — so the banner flips to "downloading…" / "installing…", with no button,
before the click's response lands — and returns ``pending``. The thread re-reports again
when it finishes, which clears the banner or puts the error and a Retry button on it.

Both are idempotent: a second click while one runs joins it rather than starting another.
"""

from __future__ import annotations

import logging

from . import chrome_install, cli_fetch, preflight

log = logging.getLogger("protoagent.plugins.agent_browser")


def _refresh(refresh) -> None:
    if callable(refresh):
        try:
            refresh()
        except Exception:  # noqa: BLE001 — the step's answer matters more than the banner
            log.exception("[agent_browser] refreshing the setup gaps failed")


def _binary(cfg: dict | None) -> str:
    return str((cfg or {}).get("binary") or preflight.DEFAULT_BINARY)


def download_cli(cfg: dict | None, refresh=None) -> dict:
    """Fetch the pinned CLI into the cache (``cli_fetch``). Retries a failed download — the
    click is the operator's explicit choice, so ``cli_autofetch`` doesn't gate it."""
    binary = _binary(cfg)
    if not preflight.is_default(binary):
        return {"ok": False, "message": (f"The plugin's `binary` setting is {binary!r}, so a downloaded "
                                         f"CLI wouldn't be the one used — clear that setting first.")}
    path, source = preflight.locate(binary)
    if path:
        _refresh(refresh)
        where = "on PATH" if source == "path" else "installed"
        return {"ok": True, "message": f"agent-browser is already {where} at {path}."}
    st = cli_fetch.ensure_cli(background=True, force=True, on_done=refresh)
    _refresh(refresh)
    version = cli_fetch.CLI_VERSION
    if st.get("state") == "fetching":
        return {"ok": True, "pending": True,
                "message": f"Downloading agent-browser v{version} — the banner updates once it's verified and installed."}
    if st.get("state") == "done":
        return {"ok": True, "message": f"agent-browser v{version} is installed."}
    return {"ok": False, "message": st.get("error") or "The download couldn't start."}


def install_chrome(cfg: dict | None, refresh=None) -> dict:
    """Run the resolved CLI's ``agent-browser install`` (``chrome_install``)."""
    exe = preflight.resolve_binary(_binary(cfg))
    if not exe:
        return {"ok": False, "message": "Install the agent-browser CLI first — Chrome is installed by it."}
    if not chrome_install.supported():
        return {"ok": False, "message": ("Chrome for Testing has no Linux ARM64 build — install Chromium "
                                         "from your package manager.")}
    st = chrome_install.start(exe, on_done=refresh)
    _refresh(refresh)
    if st.get("state") == "installing":
        return {"ok": True, "pending": True,
                "message": "Installing Chrome for Testing (a ~150 MB download) — the banner clears when it's done."}
    if st.get("state") == "done":
        return {"ok": True, "message": "Chrome for Testing is installed."}
    return {"ok": False, "message": st.get("error") or "The Chrome install couldn't start."}
