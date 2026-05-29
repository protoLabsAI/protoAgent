#!/usr/bin/env python3
"""Freeze the headless protoAgent server into a single-file Tauri sidecar.

Produces ``apps/desktop/src-tauri/binaries/protoagent-server-<target-triple>``
— the ``externalBin`` Tauri bundles and launches. Gradio is excluded (the
desktop app renders the React console itself), so the binary stays as small
as this dependency stack allows.

Run from a venv with the runtime deps + PyInstaller installed:

    pip install -r requirements.txt pyinstaller
    python apps/desktop/sidecar/build_sidecar.py

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
    ("config/langgraph-config.example.yaml", "config"),
    ("config/SOUL.md", "config"),
    ("config/soul-presets", "config/soul-presets"),
    ("static", "static"),
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
]

# Gradio (and the chat_ui module that imports it) is dead weight in headless
# mode and the worst thing to freeze — exclude it outright.
EXCLUDE = ["gradio", "chat_ui", "tkinter"]


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
        str(REPO / "server.py"),
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
