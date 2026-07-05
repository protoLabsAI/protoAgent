"""``protoagent operations`` — list the operations on the shared ops layer (ADR 0075 D2).

The CLI projection of the operations catalog (the same registry `GET /api/operations` serves),
so an operator can see every op — name, read/write, one-liner — from the terminal.
"""

from __future__ import annotations

import argparse
import json


def run_operations_cli(argv: list[str]) -> int:
    from ops import load_all

    parser = argparse.ArgumentParser(
        prog="protoagent operations", description="List the operations on the ops layer (ADR 0075 D2)."
    )
    parser.add_argument("--json", action="store_true", help="emit the raw catalog for scripting")
    args = parser.parse_args(argv)

    specs = sorted(load_all().values(), key=lambda s: s.name)
    if args.json:
        print(json.dumps([{"name": s.name, "mutates": s.mutates, "summary": s.summary} for s in specs], indent=2))
        return 0
    if not specs:
        print("no operations registered")
        return 0
    width = max(len(s.name) for s in specs)
    for s in specs:
        tag = "write" if s.mutates else "read "
        print(f"  [{tag}] {s.name:<{width}}  {s.summary}")
    return 0
