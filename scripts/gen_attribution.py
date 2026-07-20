#!/usr/bin/env python
"""Generate (and CI-verify) THIRD_PARTY_LICENSES.md from the dependency graph.

protoAgent ships under MIT (see LICENSE) but bundles third-party open-source
components. This script enumerates them so the attribution manifest can't
silently drift from what actually ships.

Two data sources, chosen so the *inventory* is deterministic and
platform-independent (which is what the CI gate depends on):

  * Python  — package list + versions from ``uv.lock`` (committed, covers every
    platform's resolution). License strings are resolved best-effort from the
    installed environment (``importlib.metadata``) and, for any that don't
    resolve, back-filled from the committed manifest itself — safe because a
    pinned ``name==version``'s license is immutable, so the existing file is a
    valid cache. That makes regeneration *monotonic*: the installed environment
    can only ever fill in an ``UNKNOWN``, never wipe an already-recorded license
    (which used to happen when regenerating in an env without the deps installed).
  * Node    — package list + versions + licenses from ``package-lock.json``
    (committed, lockfileVersion 3 carries a ``license`` field per entry). No
    ``node_modules`` walk, so platform-specific optional binaries can't skew it.

Usage:

    uv run python scripts/gen_attribution.py            # rewrite the manifest
    uv run python scripts/gen_attribution.py --check     # CI drift gate

``--check`` re-derives the inventory from the two lockfiles and compares it to
the inventory embedded in the committed manifest. It reads only committed files
— no ``uv sync`` / ``npm ci`` needed — so it is fast and can't flake on
environment differences. Regenerate after any dependency bump:

    uv sync && uv run python scripts/gen_attribution.py

Over-inclusion (listing a dev/build tool that isn't itself redistributed) is
legally harmless; under-inclusion is the risk, so this errs toward listing
everything locked.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "THIRD_PARTY_LICENSES.md"
UV_LOCK = REPO / "uv.lock"
NPM_LOCK = REPO / "package-lock.json"

# Our own packages / local sources — never attribute the project to itself.
OWN_PY = {"protoagent"}
OWN_NPM = {"protoagent-docs", "@protoagent/web", "@protoagent/desktop"}

UNKNOWN = "UNKNOWN"

# Backstop for the one case carry-forward can't rescue: seeding a fresh manifest
# in an env without the deps installed, so nothing resolves *and* there's no
# prior file to back-fill from. Refuse to write rather than commit an all-UNKNOWN
# file. Set well above the normal genuinely-unresolved rate (~5/117 ≈ 4%) so a
# healthy regen never trips it; a bare-env seed sits near 100%.
MAX_UNKNOWN_FRACTION = 0.4

# Machine-readable inventory the --check gate diffs against. Bump the version
# suffix if the embedded shape ever changes.
INVENTORY_MARKER = "ATTRIBUTION-INVENTORY-V1"

# Collapse *only* exact, legally-identical spellings. Deliberately conservative:
# we never fold "BSD License" or "Apache Software License" into a clause/version
# they don't state, since that would misrepresent the grant.
SYNONYMS = {
    "MIT License": "MIT",
    "The MIT License (MIT)": "MIT",
}


def _normalize(lic: str) -> str:
    return SYNONYMS.get(lic.strip(), lic.strip())


def _canon(name: str) -> str:
    """PEP 503 normalized key for matching a uv.lock name to installed metadata
    (folds runs of ``-``, ``_``, ``.`` — e.g. ``ruamel.yaml`` == ``ruamel-yaml``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --- Python -----------------------------------------------------------------


def _py_license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    # PEP 639: modern wheels carry a clean SPDX expression.
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    # Classifiers are the most reliable legacy signal.
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier", [])
        if c.startswith("License ::") and "OSI Approved" not in c.split("::")[-1]
    ]
    if classifiers:
        return " / ".join(dict.fromkeys(classifiers))
    # Legacy free-text License field — only trust it if it's short (an id,
    # not a pasted license body).
    raw = (meta.get("License") or "").strip()
    if raw and "\n" not in raw and len(raw) <= 40:
        return raw
    return UNKNOWN


def _installed_py_licenses() -> dict[str, str]:
    out: dict[str, str] = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if name:
            out[_canon(name)] = _py_license(dist)
    return out


def _committed_licenses() -> dict[tuple[str, str], str]:
    """Parse the current manifest's rendered tables into ``{(name, version):
    license}``. This is the carry-forward cache: a pinned ``name==version``'s
    license never changes, so a value already recorded here is authoritative
    even when the installed environment can't reproduce it."""
    if not OUT.exists():
        return {}
    out: dict[tuple[str, str], str] = {}
    for line in OUT.read_text("utf-8").splitlines():
        # Package rows render as ``| `name` | version | license |``; header and
        # separator rows (``| Package …``, ``| --- …``) don't start with a code
        # span, so this cleanly selects only real entries from both tables.
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            continue
        name, version, lic = cells[0].strip("`"), cells[1], cells[2]
        if name and version:
            out[(name, version)] = lic
    return out


def collect_python() -> tuple[list[dict], list[str]]:
    """Return ``(rows, carried)`` where ``carried`` lists the ``name==version``
    packages whose license was preserved from the committed manifest because the
    installed environment resolved them to ``UNKNOWN`` (i.e. the regressions the
    carry-forward prevented — the guard in ``main`` keys off this)."""
    data = tomllib.loads(UV_LOCK.read_text("utf-8"))
    installed = _installed_py_licenses()
    prior = _committed_licenses()
    carried: list[str] = []
    rows: dict[str, dict] = {}
    for pkg in data.get("package", []):
        name = pkg.get("name", "")
        version = pkg.get("version", "")
        if not name or name.lower() in OWN_PY:
            continue
        # Skip workspace-local sources (the project itself, editable plugins) —
        # they're first-party, not third-party to attribute.
        src = pkg.get("source", {})
        if "virtual" in src or "editable" in src:
            continue
        lic = _normalize(installed.get(_canon(name), UNKNOWN))
        if lic == UNKNOWN:
            # This env couldn't resolve a license — back-fill from the committed
            # manifest. Immutable name==version means a previously-recorded value
            # is still correct, so we never regress known -> UNKNOWN.
            prev = prior.get((name, version))
            if prev and prev != UNKNOWN:
                lic = prev
                carried.append(f"{name}=={version}")
        rows[name.lower()] = {"name": name, "version": version, "license": lic}
    return sorted(rows.values(), key=lambda r: r["name"].lower()), carried


# --- Node / npm -------------------------------------------------------------


def _npm_license(info: dict) -> str:
    lic = info.get("license")
    if isinstance(lic, str) and lic.strip():
        return lic.strip()
    if isinstance(lic, dict) and lic.get("type"):
        return str(lic["type"]).strip()
    lics = info.get("licenses")
    if isinstance(lics, list):
        types = [x.get("type", "").strip() for x in lics if isinstance(x, dict)]
        types = [t for t in types if t]
        if types:
            return " / ".join(dict.fromkeys(types))
    return UNKNOWN


def collect_npm() -> list[dict]:
    data = json.loads(NPM_LOCK.read_text("utf-8"))
    rows: dict[str, dict] = {}
    for path, info in data.get("packages", {}).items():
        # Only real installed deps live under a node_modules/ segment; the ""
        # root key and workspace paths (e.g. "apps/web") are first-party.
        if "node_modules/" not in path:
            continue
        # Workspace packages are symlinked in as links — skip them.
        if info.get("link"):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        version = info.get("version")
        if not name or not version or name in OWN_NPM:
            continue
        key = f"{name}@{version}"
        if key in rows:
            continue
        rows[key] = {
            "name": name,
            "version": version,
            "license": _normalize(_npm_license(info)),
        }
    return sorted(rows.values(), key=lambda r: (r["name"].lower(), r["version"]))


# --- Rendering --------------------------------------------------------------


def _inventory(py: list[dict], npm: list[dict]) -> dict:
    return {
        "py": sorted(f"{r['name']}=={r['version']}" for r in py),
        "npm": sorted(f"{r['name']}@{r['version']}" for r in npm),
    }


def _table(rows: list[dict]) -> str:
    if not rows:
        return "_None resolved — is the lockfile present?_\n"
    lines = ["| Package | Version | License |", "| --- | --- | --- |"]
    for r in rows:
        lines.append(f"| `{r['name']}` | {r['version']} | {r['license']} |")
    return "\n".join(lines) + "\n"


def _breakdown(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["license"]] = counts.get(r["license"], 0) + 1
    parts = [
        f"{lic} ({n})"
        for lic, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return ", ".join(parts)


def render(py: list[dict], npm: list[dict]) -> str:
    inv = json.dumps(_inventory(py, npm), separators=(",", ":"), sort_keys=True)
    return f"""# Third-Party Licenses

protoAgent is distributed under the [MIT License](./LICENSE). It bundles and
builds on the third-party open-source components listed below, each governed by
its own license. This file is **auto-generated** — do not edit by hand; run
`uv run python scripts/gen_attribution.py` after a dependency change (see that
script's header for the full recipe). CI fails if it drifts from the lockfiles.

The lists are derived from `uv.lock` and `package-lock.json`, so they reflect
the full *locked* dependency graph across platforms and intentionally
over-include build/dev tooling that is not itself redistributed — attributing
more than strictly required is harmless. Anything shown as `{UNKNOWN}` published
no machine-readable license field; consult that project directly before relying
on it.

## Python ({len(py)} packages)

License breakdown: {_breakdown(py) or "n/a"}

{_table(py)}
## Node / npm ({len(npm)} packages)

License breakdown: {_breakdown(npm) or "n/a"}

{_table(npm)}
<!-- {INVENTORY_MARKER} {inv} -->
"""


def _embedded_inventory(text: str) -> dict | None:
    marker = f"<!-- {INVENTORY_MARKER} "
    for line in text.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):].rsplit(" -->", 1)[0])
    return None


def _check(py: list[dict], npm: list[dict]) -> int:
    if not OUT.exists():
        print(f"::error::{OUT.name} is missing — run scripts/gen_attribution.py")
        return 1
    fresh = _inventory(py, npm)
    embedded = _embedded_inventory(OUT.read_text("utf-8"))
    if embedded is None:
        print(f"::error::{OUT.name} has no {INVENTORY_MARKER} — regenerate it")
        return 1
    if embedded == fresh:
        print(f"ok: {OUT.name} matches uv.lock + package-lock.json "
              f"({len(py)} Python + {len(npm)} npm)")
        return 0
    for eco in ("py", "npm"):
        added = sorted(set(fresh[eco]) - set(embedded[eco]))
        removed = sorted(set(embedded[eco]) - set(fresh[eco]))
        for a in added:
            print(f"  + {eco}: {a}")
        for r in removed:
            print(f"  - {eco}: {r}")
    print(f"::error::{OUT.name} is stale vs the lockfiles. "
          f"Run: uv sync && uv run python scripts/gen_attribution.py")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify the manifest matches the lockfiles (CI gate); "
                         "reads only committed files, no install needed")
    args = ap.parse_args()

    (py, carried), npm = collect_python(), collect_npm()

    if args.check:
        return _check(py, npm)

    # Loud, non-fatal: a healthy regen carries nothing. Anything here means this
    # env couldn't resolve those licenses and we fell back to the committed file.
    if carried:
        shown = ", ".join(carried[:8]) + (" …" if len(carried) > 8 else "")
        print(f"::warning::preserved {len(carried)} license(s) from the existing "
              f"{OUT.name} — this env couldn't resolve them (deps not installed?). "
              f"Regenerate after `uv sync` to refresh: {shown}", file=sys.stderr)

    # Backstop the seed-in-bare-env case carry-forward can't save (nothing to
    # back-fill from). Don't overwrite a manifest with a mostly-UNKNOWN one.
    unknown = sum(1 for r in py if r["license"] == UNKNOWN)
    if py and unknown / len(py) > MAX_UNKNOWN_FRACTION:
        print(f"::error::refusing to write {OUT.name}: {unknown}/{len(py)} Python "
              f"licenses are {UNKNOWN} — this looks like an environment without the "
              f"project deps. Run: uv sync && uv run python scripts/gen_attribution.py",
              file=sys.stderr)
        return 1

    OUT.write_text(render(py, npm), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(REPO)}: {len(py)} Python + {len(npm)} npm packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
