"""A plugin view's `--pl-*` references must name tokens the DS actually defines.

The friction view styled its severity accents with `--pl-color-danger` — a token
@protolabsai/ui has never defined (the real name is `--pl-color-status-error`).
CSS resolves an undefined custom property with no fallback to *invalid at
computed-value time*, so the declaration falls back to the property's default,
silently:

    .badge.sev-major { background: var(--pl-color-danger); color: var(--pl-color-fg-on-accent); }

...rendered a transparent background with `#0a0a0c` text on the `#0a0a0c` page —
a 1.0:1 "MAJOR" badge, invisible in dark mode and white-on-white in light. The
sibling `border-color` fell back to `currentColor`, ringing every major entry in
near-white. Nothing failed; it just could not be read.

Two checks, because the kit itself is an npm artifact and is NOT committed:

  * `test_no_known_misspelled_ds_tokens` always runs. It denies the specific
    invented names we have actually shipped, and names the real token in the
    failure. A misspelling behind a fallback still fails: it renders as the
    hardcoded fallback forever and never re-themes with the operator's theme,
    which is the whole point of linking the kit.
  * `test_every_referenced_token_is_defined` is the general rule, and can only
    run where `plugin-kit.css` resolves (a checkout with the web deps installed).
    It is skipped otherwise ON PURPOSE — an always-skipping guard would be no
    guard at all, which is why the denylist above carries the load in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Names we have shipped that the DS does not define → the token that is meant.
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


def _surfaces() -> list[Path]:
    """Every bundled file that can style a plugin view: the pages themselves, the
    module scripts they load, and the Python modules that still inline a page."""
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


def _kit_css() -> Path | None:
    for candidate in (
        REPO / "apps" / "web" / "public" / "_ds" / "plugin-kit.css",
        REPO / "apps" / "web" / "dist" / "_ds" / "plugin-kit.css",
        REPO / "apps" / "web" / "node_modules" / "@protolabsai" / "ui" / "plugin-kit.css",
    ):
        if candidate.is_file():
            return candidate
    return None


def test_every_referenced_token_is_defined() -> None:
    kit = _kit_css()
    if kit is None:
        pytest.skip("plugin-kit.css not resolvable (needs `npm ci` in apps/web)")
    defined = set(_TOKEN_DEF.findall(kit.read_text(encoding="utf-8")))
    assert "--pl-color-status-error" in defined, f"{kit} does not look like the DS kit"
    bad: list[str] = []
    for path in _surfaces():
        for name in sorted(_referenced(path) - defined):
            bad.append(f"{path.relative_to(REPO)}: {name}")
    assert not bad, (
        "plugin views reference --pl-* tokens the DS kit does not define:\n  " + "\n  ".join(bad)
    )
