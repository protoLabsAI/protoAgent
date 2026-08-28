#!/usr/bin/env python3
"""Report what a branch's `uv.lock` actually does — and fail on silent downgrades.

A dependency PR is titled after ONE package. Its lockfile is not so disciplined:
raising one pin can force others DOWN, because a resolver will happily satisfy a
new constraint by walking something else backwards.

That is not hypothetical. #3218 was titled "bump websockets from 15.0.1 to
17.0.1" and its lock diff was 172 lines — it also took `langchain` 1.3.18 ->
1.3.2, `langgraph` 1.2.11 -> 1.2.2 and `langgraph-sdk` 0.4.2 -> 0.3.15, because
websockets 17 is only resolvable once langgraph-sdk drops below 0.4.2 (0.4.x
caps `websockets<17`). It was reviewed from its title and its green checks, and
merged. The core graph runtime went backwards for four hours and every gate
stayed green, because a downgrade is not a test failure — it is a silently
older product.

So: downgrades fail, and have to be said out loud. Everything else is reported.

    python scripts/lock_diff.py                      # vs origin/main
    python scripts/lock_diff.py --base <ref>
    python scripts/lock_diff.py --markdown           # table, for a PR comment

Downgrades exit 1. When one is *intended* — the langgraph/websockets trade is a
real example, where the runtime matters more than the transitive — apply the
`lock-downgrade-ok` label and say why in the PR. Removals and additions are
reported but never fail: a dependency legitimately dropping an extra (ddgs 9.16
dropping brotli/h2/socksio) is routine, and gating on it would train people to
ignore this.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _versions(raw: str) -> dict[str, str]:
    return {p["name"]: p.get("version", "") for p in tomllib.loads(raw).get("package", []) if p.get("name")}


def _at(ref: str) -> dict[str, str]:
    out = subprocess.run(
        ["git", "show", f"{ref}:uv.lock"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"lock_diff: cannot read uv.lock at {ref!r}: {out.stderr.strip()}")
    return _versions(out.stdout)


def _is_downgrade(before: str, after: str) -> bool:
    """True when `after` sorts below `before`.

    Uses `packaging` when it is importable (it is a transitive of the project) and
    degrades to a numeric-tuple compare otherwise — a comparison this script
    cannot make is reported as "not a downgrade", because blocking a PR on our own
    parsing failure would be worse than missing an exotic version scheme.
    """
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(after) < Version(before)
        except InvalidVersion:
            return False
    except ImportError:
        pass

    def parts(v: str) -> list[int]:
        return [int(x) for x in v.replace("-", ".").split(".") if x.isdigit()]

    try:
        return parts(after) < parts(before)
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main", help="ref to compare against (default: origin/main)")
    ap.add_argument("--markdown", action="store_true", help="emit a markdown table")
    ap.add_argument("--allow-downgrades", action="store_true", help="report downgrades without failing")
    args = ap.parse_args()

    base = _at(args.base)
    head = _versions((ROOT / "uv.lock").read_text())

    downgraded, upgraded, added, removed = [], [], [], []
    for name in sorted(set(base) | set(head)):
        before, after = base.get(name), head.get(name)
        if before == after:
            continue
        if before is None:
            added.append((name, after))
        elif after is None:
            removed.append((name, before))
        elif _is_downgrade(before, after):
            downgraded.append((name, before, after))
        else:
            upgraded.append((name, before, after))

    if not (downgraded or upgraded or added or removed):
        print(f"lock_diff: uv.lock is unchanged vs {args.base}")
        return 0

    if args.markdown:
        print(f"| package | {args.base} | this branch | |")
        print("|---|---|---|---|")
        for n, a, b in downgraded:
            print(f"| `{n}` | {a} | **{b}** | ⬇ **downgrade** |")
        for n, a, b in upgraded:
            print(f"| `{n}` | {a} | {b} | ⬆ |")
        for n, v in added:
            print(f"| `{n}` | — | {v} | + added |")
        for n, v in removed:
            print(f"| `{n}` | {v} | — | − removed |")
    else:
        print(f"lock_diff: uv.lock vs {args.base}")
        for label, rows in (("DOWNGRADED", downgraded), ("upgraded", upgraded)):
            for n, a, b in rows:
                print(f"  {label:>10}  {n:28} {a} -> {b}")
        for n, v in added:
            print(f"  {'added':>10}  {n:28} {v}")
        for n, v in removed:
            print(f"  {'removed':>10}  {n:28} was {v}")

    if downgraded and not args.allow_downgrades:
        names = ", ".join(n for n, _, _ in downgraded)
        print(
            f"\n::error::uv.lock DOWNGRADES {len(downgraded)} package(s): {names}. "
            "A bump that walks something else backwards is how the core runtime silently "
            "regressed in #3218. If this is intended, say so in the PR and apply the "
            "`lock-downgrade-ok` label.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
