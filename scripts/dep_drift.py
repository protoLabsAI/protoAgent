#!/usr/bin/env python3
"""Report where the INSTALLED dependency set differs from `uv.lock`.

protoAgent pins dependencies in `uv.lock`, but a `pip install -r requirements.txt`
(what the Docker image, the desktop sidecar and a plain contributor checkout all
do) resolves whatever is newest. Those two answers drifted a long way apart once —
langchain-openai 1.3.0 in the lock vs 1.6.0 resolved, with `openai` and `anthropic`
each a full major apart — and because the local suite ran one set and CI ran the
other, a defect could pass locally and then fail a PR that had not caused it.

This script names that gap. It is what the upstream canary posts when the suite
breaks against latest, and it answers "is this me or is this upstream?" locally:

    python scripts/dep_drift.py                  # human-readable
    python scripts/dep_drift.py --markdown       # table, for an issue body
    python scripts/dep_drift.py --only langchain langgraph openai anthropic

Exit code is 0 whether or not there is drift — drift is information, not failure.
Pass ``--exit-code`` to make any drift a non-zero exit (for a gate).
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The stack whose drift actually changes protoAgent's behavior — the model
# clients and the graph runtime. Everything else is reported under `--all`.
AI_STACK_PREFIXES = ("langchain", "langgraph", "langsmith")
AI_STACK_EXACT = {"openai", "anthropic", "langfuse", "tiktoken"}


def locked_versions() -> dict[str, str]:
    """`{name: version}` for every package pinned in uv.lock."""
    data = tomllib.loads((ROOT / "uv.lock").read_text())
    return {p["name"]: p.get("version", "") for p in data.get("package", []) if p.get("name")}


def installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def is_ai_stack(name: str) -> bool:
    return name.startswith(AI_STACK_PREFIXES) or name in AI_STACK_EXACT


def collect(only: list[str] | None, everything: bool) -> list[tuple[str, str, str]]:
    """`(name, locked, installed)` for packages whose versions disagree."""
    rows = []
    for name, locked in sorted(locked_versions().items()):
        if only:
            if not any(name.startswith(prefix) for prefix in only):
                continue
        elif not everything and not is_ai_stack(name):
            continue
        found = installed_version(name)
        if found is None or found == locked:
            continue
        rows.append((name, locked, found))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--markdown", action="store_true", help="emit a markdown table")
    parser.add_argument("--all", action="store_true", help="every package, not just the AI stack")
    parser.add_argument("--only", nargs="*", help="name prefixes to report on")
    parser.add_argument("--exit-code", action="store_true", help="exit 1 when there is drift")
    args = parser.parse_args()

    rows = collect(args.only, args.all)
    scope = "dependency set" if args.all else "AI stack"

    if not rows:
        print(f"no drift: the installed {scope} matches uv.lock")
        return 0

    if args.markdown:
        print("| package | uv.lock | installed |")
        print("|---|---|---|")
        for name, locked, found in rows:
            print(f"| `{name}` | {locked} | **{found}** |")
    else:
        width = max(len(name) for name, _, _ in rows)
        print(f"{len(rows)} package(s) in the {scope} differ from uv.lock:\n")
        for name, locked, found in rows:
            print(f"  {name:<{width}}  {locked:>12}  ->  {found}")

    return 1 if args.exit_code else 0


if __name__ == "__main__":
    sys.exit(main())
