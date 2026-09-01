"""A plugin view's `--pl-*` references must name tokens the DS actually defines.

The friction view styled its severity accents with `--pl-color-danger` — a token
@protolabsai/ui has never defined (the real name is `--pl-color-status-error`). CSS
resolves an undefined custom property with no fallback to *invalid at computed-value
time*, so the declaration falls back to the property's default, silently:

    .badge.sev-major { background: var(--pl-color-danger); color: var(--pl-color-fg-on-accent); }

...rendered a transparent background with `#0a0a0c` text on the `#0a0a0c` page — a 1.0:1
"MAJOR" badge, invisible in dark mode and white-on-white in light. The sibling
`border-color` fell back to `currentColor`, ringing every major entry in near-white.
Nothing failed; it just could not be read.

The kit is an npm artifact and is NOT committed, so the real check — "every referenced
token is one the DS defines" — could only run where `npm ci` had been run, i.e. never in
the Python CI job. A guard that always skips is not a guard. So the token list is
committed as a snapshot (`tests/data/ds-plugin-kit-tokens.txt`) and the rule runs
everywhere; a second test keeps the snapshot honest wherever the real kit DOES resolve.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO / "tests" / "data" / "ds-plugin-kit-tokens.txt"

# Names we have shipped that the DS does not define → the token that was meant. Redundant
# with the snapshot check below, and kept anyway: it names the RIGHT token in the failure
# message, which is the difference between a fix and a guess.
KNOWN_MISSPELLINGS = {
    "--pl-color-danger": "--pl-color-status-error",
    "--pl-color-success": "--pl-color-status-success",
    "--pl-color-warning": "--pl-color-status-warning",
    "--pl-color-info": "--pl-color-status-info",
    "--pl-radius-md": "--pl-radius",
    "--pl-radius-sm": "--pl-radius",
}

_VAR_REF = re.compile(r"var\(\s*(--pl-[a-z0-9-]+)")
_TOKEN_DEF = re.compile(r"^\s*(--pl-[a-z0-9-]+)\s*:", re.M)


def _defined_tokens() -> set[str]:
    return {
        line.strip()
        for line in SNAPSHOT.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("--pl-")
    }


def _surfaces() -> list[Path]:
    """Every bundled file that can style a plugin view: the pages themselves, the module
    scripts they load, and the Python modules that still inline a page."""
    roots = (REPO / "plugins", REPO / "examples" / "plugins", REPO / "graph" / "plugins")
    out: list[Path] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".html", ".js", ".py"} or not path.is_file():
                continue
            if "vendor" in path.parts or "node_modules" in path.parts:
                continue  # third-party bundles are not ours to re-token
            out.append(path)
    return out


def _referenced(path: Path) -> set[str]:
    try:
        return set(_VAR_REF.findall(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return set()


def test_no_known_misspelled_ds_tokens() -> None:
    bad: list[str] = []
    for path in _surfaces():
        for name in sorted(_referenced(path) & KNOWN_MISSPELLINGS.keys()):
            bad.append(f"{path.relative_to(REPO)}: {name} → use {KNOWN_MISSPELLINGS[name]}")
    assert not bad, "plugin views reference DS tokens that do not exist:\n  " + "\n  ".join(bad)


def test_every_referenced_token_is_defined() -> None:
    """The general rule, running everywhere because the token list is committed.

    A misspelling behind a fallback still fails: it renders as the hardcoded fallback
    forever and never re-themes with the operator's theme, which is the whole point of
    linking the kit."""
    defined = _defined_tokens()
    assert "--pl-color-status-error" in defined, f"{SNAPSHOT} does not look like the token list"
    bad: list[str] = []
    for path in _surfaces():
        for name in sorted(_referenced(path) - defined):
            bad.append(f"{path.relative_to(REPO)}: {name}")
    assert not bad, (
        "plugin views reference --pl-* tokens the DS kit does not define:\n  "
        + "\n  ".join(bad)
        + "\n(if the DS added one, refresh: python scripts/gen_ds_token_snapshot.py)"
    )


def _kit_css() -> Path | None:
    for candidate in (
        REPO / "apps" / "web" / "public" / "_ds" / "plugin-kit.css",
        REPO / "apps" / "web" / "dist" / "_ds" / "plugin-kit.css",
        REPO / "apps" / "web" / "node_modules" / "@protolabsai" / "ui" / "plugin-kit.css",
    ):
        if candidate.is_file():
            return candidate
    return None


def test_the_token_snapshot_matches_the_real_kit() -> None:
    """Keeps the snapshot from rotting into fiction.

    Skips where the kit isn't installed — but unlike the rule above, skipping here is
    harmless: a stale snapshot can only cause a FALSE FAILURE (a real new token flagged as
    undefined), never a false pass on the misspellings we actually ship."""
    kit = _kit_css()
    if kit is None:
        pytest.skip("plugin-kit.css not resolvable (needs `npm ci --prefix apps/web`)")
    actual = set(_TOKEN_DEF.findall(kit.read_text(encoding="utf-8")))
    snapshot = _defined_tokens()
    assert actual == snapshot, (
        "tests/data/ds-plugin-kit-tokens.txt is stale.\n"
        f"  added upstream: {sorted(actual - snapshot)}\n"
        f"  gone upstream:  {sorted(snapshot - actual)}\n"
        "  refresh: python scripts/gen_ds_token_snapshot.py"
    )
