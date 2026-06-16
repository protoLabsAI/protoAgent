#!/usr/bin/env python3
"""Verify a bundle's pin set against a protoAgent checkout (ADR 0049).

Installs the bundle (at its pinned refs) into a SCRATCH agent, enables every member,
loads them through the real plugin loader, and probes every declared console-view
path over HTTP — the check that catches "the pin predates the view fix" before an
operator spawns a broken archetype.

Run from inside a protoAgent checkout with deps synced (the verify workflow does
exactly this):

    uv run --no-sync python /path/to/bundle/scripts/verify_bundle.py /path/to/bundle

The bundle path must be a git repo (a CI checkout is); the installer clones from it.
Exit code 0 = every member installed, loaded, and every declared view answered 200.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main(bundle_src: str) -> int:
    # The cwd is the protoAgent checkout (`python <script>` puts the SCRIPT's dir on
    # sys.path, not the cwd — `graph` wouldn't import without this).
    sys.path.insert(0, os.getcwd())
    # Scope EVERYTHING to a throwaway dir before importing graph modules — the
    # installer reads PROTOAGENT_PLUGINS_LOCK at import time, the rest at call time.
    scratch = Path(tempfile.mkdtemp(prefix="bundle-verify-"))
    (scratch / "cfg").mkdir()
    os.environ["PROTOAGENT_CONFIG_DIR"] = str(scratch / "cfg")
    os.environ["PROTOAGENT_PLUGINS_DIR"] = str(scratch / "cfg" / "plugins")
    os.environ["PROTOAGENT_PLUGINS_LOCK"] = str(scratch / "plugins.lock")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from graph.config import LangGraphConfig
    from graph.plugins import installer
    from graph.plugins.loader import load_plugins, load_manifest

    failures: list[str] = []

    # ── 1. INSTALL the bundle at its pinned refs (fan-out, ADR 0040) ─────────────
    print(f"installing bundle from {bundle_src} …")
    summary = installer.install(bundle_src)
    if "bundle" not in summary:
        print("FAIL: source is a single plugin, not a bundle manifest")
        return 1
    members = [s["id"] for s in summary["installed"]]
    builtins = summary["skipped_builtin"]
    for s in summary["installed"]:
        print(f"  installed {s['id']}@{s['resolved_sha'][:10]} (ref {s.get('requested_ref') or 'HEAD'})")

    # ── 2. ENABLE everything the bundle suggests + LOAD through the real loader ──
    enabled = list(dict.fromkeys((summary.get("enabled") or []) + members))
    cfg_path = scratch / "cfg" / "langgraph-config.yaml"
    cfg_path.write_text(
        "plugins:\n  enabled: [" + ", ".join(enabled) + f"]\n  plugins_dir: {scratch / 'cfg' / 'plugins'}\n"
    )
    res = load_plugins(LangGraphConfig.from_yaml(str(cfg_path)))
    meta_by_id = {m["id"]: m for m in res.meta}
    for pid in members + builtins:
        m = meta_by_id.get(pid)
        if m is None:
            failures.append(f"{pid}: not found by the loader")
        elif not m.get("loaded"):
            failures.append(f"{pid}: failed to load — {m.get('error')}")
        else:
            print(f"  loaded {pid}: {len(m.get('tools') or [])} tool(s)")

    # ── 3. PROBE every declared console-view path over HTTP ──────────────────────
    app = FastAPI()
    for r in res.routers:
        app.include_router(r["router"], prefix=r.get("prefix", ""))
    client = TestClient(app)
    root = installer.live_plugins_dir()
    for pid in members:
        manifest = load_manifest(root / pid)
        for view in manifest.views if manifest else []:
            path = view.get("path", "")
            if not path:
                continue
            status = client.get(path).status_code
            ok = status == 200
            print(f"  view {pid} {path!r} -> {status}{'' if ok else '  ✗'}")
            if not ok:
                failures.append(f"{pid}: view {path!r} returned {status}")

    if failures:
        print(f"\nFAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nOK — {len(members)} member(s) installed+loaded, all declared views serve 200.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
