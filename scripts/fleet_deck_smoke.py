#!/usr/bin/env python3
"""Desktop-build smoke for the fleet deck (#3498): run a FROZEN sidecar's ``fleet`` verbs.

``build_sidecar.py`` bundles Textual and the ``deck`` package so ``protoagent-server fleet``
opens the deck from the desktop binary — on a desktop-only install, the only ``protoagent``
on the box. ``live_smoke.py --bin`` boots the server and never touches the deck, so a
PyInstaller miss in it shipped green. This runs, against the same binary:

- ``fleet --help``                  exit 0, the fleet usage (the verb table forwards)
- ``fleet ls --offline --json``     exit 0, ``{"mode": "offline", "agents": [...]}``
                                    (``deck.hub`` / ``deck.data`` + the supervisor)
- ``fleet --all --offline --json``  exit 0, ``{"mode": "hubs", "hubs": [...]}``
                                    (``deck.discovery``, reached by name)
- ``fleet --self-check``            exit 0 — the Textual deck, opened under the headless
                                    driver, painting its first screens (``deck.selfcheck``)

No hub is needed: everything is offline, in an empty throwaway instance + box root. Without
``--bin`` it runs ``python -m server`` instead, so the checks themselves are testable in the
normal suite. Exit 0 when every check passes, 1 (with each failure's output) otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent

for _stream in (sys.stdout, sys.stderr):  # a Windows pipe defaults to cp1252
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _json_with(mode: str, key: str) -> Callable[[str], str | None]:
    def check(out: str) -> str | None:
        try:
            doc: Any = json.loads(out)
        except ValueError as exc:
            return f"stdout is not JSON ({exc})"
        if not isinstance(doc, dict) or doc.get("mode") != mode or not isinstance(doc.get(key), list):
            return f"expected an object with mode={mode!r} and a {key!r} list"
        return None

    return check


def _contains(text: str) -> Callable[[str], str | None]:
    return lambda out: None if text in out else f"stdout lacks {text!r}"


CHECKS: list[tuple[list[str], Callable[[str], str | None]]] = [
    (["fleet", "--help"], _contains("usage: protoagent fleet")),
    (["fleet", "ls", "--offline", "--json"], _json_with("offline", "agents")),
    (["fleet", "--all", "--offline", "--json"], _json_with("hubs", "hubs")),
    (["fleet", "--self-check"], _contains("fleet deck self-check ok")),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="fleet deck smoke (desktop build)")
    ap.add_argument("--bin", dest="bin_path", help="the frozen sidecar to smoke (default: `python -m server`)")
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds per command (a onefile binary self-extracts every run)")
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="deck-smoke-home-"))
    box = Path(tempfile.mkdtemp(prefix="deck-smoke-box-"))
    env = {
        **os.environ,
        "PROTOAGENT_HOME": str(home),
        "PROTOAGENT_BOX_ROOT": str(box),
        "PROTOAGENT_DISCOVERY_DISABLE": "1",
    }
    if args.bin_path:
        # Neutral cwd, no PYTHONPATH: the checkout must not paper over under-collection.
        env.pop("PYTHONPATH", None)
        base, cwd = [str(Path(args.bin_path).resolve())], str(home)
    else:
        env["PYTHONPATH"] = str(ROOT)
        base, cwd = [sys.executable, "-m", "server"], str(ROOT)

    failed = 0
    for argv, check in CHECKS:
        label = " ".join(argv)
        try:
            proc = subprocess.run(
                [*base, *argv], env=env, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"✗ {label}: no exit within {args.timeout:.0f}s")
            failed += 1
            continue
        problem = f"exit {proc.returncode}" if proc.returncode != 0 else check(proc.stdout)
        if problem is None:
            print(f"✓ {label}")
            continue
        failed += 1
        print(f"✗ {label}: {problem}")
        print(f"  --- stdout (tail) ---\n{proc.stdout[-2000:]}\n  --- stderr (tail) ---\n{proc.stderr[-4000:]}")
    print(f"fleet deck smoke: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
