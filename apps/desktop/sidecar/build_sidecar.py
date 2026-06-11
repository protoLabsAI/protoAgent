#!/usr/bin/env python3
"""Freeze the headless protoAgent server into a single-file Tauri sidecar.

Produces ``apps/desktop/src-tauri/binaries/protoagent-server-<target-triple>``
— the ``externalBin`` Tauri bundles and launches. The desktop app renders the
React console itself, so the binary stays as small as this dependency stack allows.

Run from a venv with the runtime deps + PyInstaller installed:

    pip install -r requirements.txt pyinstaller "mcp[cli]"
    python apps/desktop/sidecar/build_sidecar.py

(``mcp[cli]`` is a build-env-only need: ``--collect-all mcp`` imports every mcp
submodule, and ``mcp.cli`` raises at import when typer is missing.)

The target triple matches Tauri's expectation (``rustc`` host), so the binary
lands at the exact name Tauri looks for during ``tauri build``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BINARIES_DIR = REPO / "apps" / "desktop" / "src-tauri" / "binaries"
NAME = "protoagent-server"

# Read-only defaults the frozen server reads via REPO_ROOT/config (which
# resolves to _MEIPASS/config inside the bundle). Live state (langgraph-config
# .yaml, secrets.yaml, .setup-complete) is NOT bundled — it's written at
# runtime to PROTOAGENT_CONFIG_DIR. Never bundle secrets.yaml or the live YAML.
BUNDLED_DATA: list[tuple[str, str]] = [
    # Version truth (version-coherence Cross-cutting B, docs/dev/version-coherence.md):
    # `paths.package_version()` has no installed-package metadata in a frozen binary,
    # so it falls back to reading `pyproject.toml` at _MEIPASS. Bundle it (top level)
    # so the desktop app reports its REAL version instead of "0.0.0" — which otherwise
    # blinds the A2A card, the fleet version handshake, runtime status, and the plugin
    # `min_protoagent_version` compat gate (every plugin with one is wrongly refused).
    ("pyproject.toml", "."),
    ("config/langgraph-config.example.yaml", "config"),
    ("config/SOUL.md", "config"),
    ("config/soul-presets", "config/soul-presets"),
    ("static", "static"),
    # First-party plugins (ADR 0018/0019) — incl. the Discord + Google surfaces.
    # Loaded by file path (importlib) at runtime, so PyInstaller's import-scan
    # misses them; ship the tree as data so the frozen app finds them under
    # _MEIPASS/plugins (the loader's bundle root).
    ("plugins", "plugins"),
]

# Packages PyInstaller's static analysis under-collects (dynamic imports +
# importlib.metadata entry points). --collect-all pulls modules + data + metadata.
COLLECT_ALL = [
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "ddgs",
    "langfuse",
    "croniter",
    # A2A 1.0 (ADR 0014): the SDK + the protoLabs conventions layer (git-dep).
    # Both pull submodules/metadata that a bare import-scan misses — without a
    # full collect, the frozen `protolabs_a2a` is missing `build_agent_card`.
    "a2a",
    "protolabs_a2a",
    # a2a-sdk[sqlite] durable task/push stores: imported lazily by the SDK, so
    # the import-scan misses them — collect explicitly.
    "aiosqlite",
    "sqlalchemy",
    # The Discord surface (ADR 0015) is loaded by the discord plugin (and the
    # gateway imports ``websockets`` lazily), so the import-scan misses it.
    # Collect both so Discord works in the frozen app.
    "surfaces",
    "websockets",
    # The `tools` package — the discord plugin imports `tools.discord_tools`
    # (and future plugins may import other tool modules). Plugins are loaded by
    # file path, so PyInstaller's import-scan never sees these; collect the whole
    # package so plugin-only tool imports resolve in the frozen app.
    "tools",
    # Google surface (ADR 0017): the MCP SDK (FastMCP) + the repo's google MCP
    # server module, loaded by the google plugin and re-invoked frozen via the
    # ``--mcp-plugin google`` self-reinvoke entry.
    "mcp",
    "mcp_servers",
]

# Google client libraries (ADR 0017) — bundled only when installed in the build
# env (``requirements-google.txt``). Keeps a lean build (no google) working;
# install the extra to ship Gmail/Calendar in the frozen app.
OPTIONAL_COLLECT_ALL = [
    "google.auth",
    "google.oauth2",
    "google_auth_oauthlib",
    "googleapiclient",
]

# tkinter is GUI dead weight in a headless server and a freeze hazard — exclude it.
# (Gradio + the chat_ui module it backed were removed from the project entirely.)
EXCLUDE = ["tkinter"]


def target_triple() -> str:
    """Tauri names sidecars ``<bin>-<rustc-host-triple>``; match it."""
    out = subprocess.run(["rustc", "-Vv"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("could not determine rustc host triple (is rustc installed?)")


def main() -> None:
    triple = target_triple()
    work = REPO / "build" / "sidecar"
    sep = ";" if os.name == "nt" else ":"

    add_data: list[str] = []
    for src, dest in BUNDLED_DATA:
        if (REPO / src).exists():
            add_data += ["--add-data", f"{REPO / src}{sep}{dest}"]
        else:
            print(f"warning: bundled data missing, skipping: {src}", file=sys.stderr)

    collect: list[str] = []
    for pkg in COLLECT_ALL:
        collect += ["--collect-all", pkg]
    # Optional Google libs: collect only what's importable in this build env.
    import importlib.util

    for pkg in OPTIONAL_COLLECT_ALL:
        if importlib.util.find_spec(pkg) is not None:
            collect += ["--collect-all", pkg]
        else:
            print(f"note: optional package not installed, skipping: {pkg} "
                  "(install requirements-google.txt to ship Gmail/Calendar)",
                  file=sys.stderr)
    exclude: list[str] = []
    for mod in EXCLUDE:
        exclude += ["--exclude-module", mod]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconfirm", "--clean",
        "--name", NAME,
        "--distpath", str(work / "dist"),
        "--workpath", str(work / "build"),
        "--specpath", str(work),
        *exclude, *add_data, *collect,
        # ADR 0023: server.py is now the `server` package; freeze its module
        # entry point. PyInstaller bundles the whole `server` package and the
        # `--add-data` assets land at _MEIPASS top level (server/_bundle_root
        # resolves there when frozen).
        str(REPO / "server" / "__main__.py"),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO))

    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    built = work / "dist" / f"{NAME}{suffix}"
    dest = BINARIES_DIR / f"{NAME}-{triple}{suffix}"
    shutil.copy2(built, dest)
    os.chmod(dest, 0o755)
    print(f"\nsidecar -> {dest}")


if __name__ == "__main__":
    main()
