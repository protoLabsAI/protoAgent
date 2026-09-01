#!/usr/bin/env python3
"""Refresh tests/data/ds-plugin-kit-tokens.txt from the installed @protolabsai/ui kit.

The DS plugin-kit is an npm artifact and is not committed, so the token guard
(tests/test_plugin_view_ds_tokens.py) checks plugin views against this snapshot — which is
the only thing that lets it run in the Python CI job at all. Run this after bumping
@protolabsai/ui; the snapshot test tells you when it is stale.

    npm ci --prefix apps/web && python scripts/gen_ds_token_snapshot.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO / "tests" / "data" / "ds-plugin-kit-tokens.txt"
CANDIDATES = (
    REPO / "apps" / "web" / "public" / "_ds" / "plugin-kit.css",
    REPO / "apps" / "web" / "dist" / "_ds" / "plugin-kit.css",
    REPO / "apps" / "web" / "node_modules" / "@protolabsai" / "ui" / "plugin-kit.css",
)


def main() -> int:
    kit = next((c for c in CANDIDATES if c.is_file()), None)
    if kit is None:
        print("plugin-kit.css not found — run `npm ci --prefix apps/web` first", file=sys.stderr)
        return 1
    names = sorted(set(re.findall(r"^\s*(--pl-[a-z0-9-]+)\s*:", kit.read_text(encoding="utf-8"), re.M)))
    header = [ln for ln in SNAPSHOT.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]
    SNAPSHOT.write_text("\n".join(header + names) + "\n", encoding="utf-8")
    print(f"wrote {len(names)} tokens from {kit.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
