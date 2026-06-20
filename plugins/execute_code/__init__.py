"""execute_code plugin — programmatic tool-calling as an opt-in plugin.

Moved out of the lean core (bd-37i). The model writes ONE Python script that
calls several of its tools and returns just the result — collapsing a long
think→call→read chain into a single turn. The script runs in an isolated
subprocess with a scrubbed environment (no credentials) and a hard timeout;
tools are invoked back in the parent over an fd-based RPC bridge.

This is **model-authored code execution** — isolation, NOT a true sandbox (the
script can still reach the disk/network as the server user) — so it ships
DISABLED; enable only for a trusted model or inside a hardened container.

It uses the late-tools seam (``register_late_tool_factory``) because the tool
must proxy the FULLY assembled toolset, which a normal ``register_tool`` can't
see. Not loaded in the packaged desktop build (no standalone Python interpreter).
"""

from __future__ import annotations

import logging
import sys

from .engine import build_execute_code_tool

log = logging.getLogger("protoagent.plugins.execute_code")


def register(registry) -> None:
    """Wire execute_code as a late tool (ADR 0001 + the late-tools seam)."""
    cfg = registry.config  # the plugin's `execute_code` config section (ADR 0019)

    # Frozen desktop build: spawning a Python subprocess needs a standalone
    # interpreter the PyInstaller binary doesn't ship. Don't register the tool
    # there at all — it simply won't exist (matches the old core behavior).
    if getattr(sys, "frozen", False):
        log.info("[execute_code] packaged desktop build — no standalone Python, tool not loaded")
        return

    def _factory(all_tools, config):
        return build_execute_code_tool(
            all_tools,
            tools=cfg.get("tools") or None,
            timeout=float(cfg.get("timeout", 30.0)),
            truncate=int(cfg.get("output_truncate", 6000)),
        )

    registry.register_late_tool_factory(_factory)
