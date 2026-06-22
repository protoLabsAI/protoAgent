"""Drift guard: requirements-core.txt ⊇ the core pyproject [project.dependencies].

``pyproject.toml [project.dependencies]`` is the single source of truth for the
runtime deps (``pip install .`` / ``uv sync`` read it). But the Docker image
installs from ``requirements-core.txt`` (``pip install -r requirements-core.txt``),
which is hand-mirrored — so a dep added only to pyproject passes every CI gate
(tests run against the pyproject install) yet silently MISSES the production
image. That's the #874 dep-drift class: the runtime image quietly lacks a package
the agent imports, and the failure only surfaces in production.

This test makes that drift a CI failure: every package name declared in
``[project.dependencies]`` must also appear in ``requirements-core.txt``. It
compares by *normalized package name* (PEP 503) only — version pins and extras
are formatting and intentionally NOT asserted, so a ``>=`` vs ``==`` or an extras
list difference doesn't cause false failures.

CORE-SUBSET DEFINITION: today the ENTIRE ``[project.dependencies]`` table IS the
core tier — pyproject's own comment marks it "core (the `none`/`console` tiers) —
mirrors requirements-core.txt", and there is no separate full-tier section. If a
full-tier-only section is ever added to pyproject, mark those deps so they can be
excluded from the core subset here (see ``_FULL_TIER_ONLY`` below).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_REQUIREMENTS_CORE = _REPO_ROOT / "requirements-core.txt"

# Full-tier-only deps to exclude from the "core subset". Empty today — the whole
# [project.dependencies] table is the core tier (the lean none/console tiers).
# If a full-only section is added to pyproject, list its package names here (PEP
# 503 canonical form) with a comment, so this guard only enforces the core subset.
_FULL_TIER_ONLY: set[str] = set()


def _canonical_names(specifiers: list[str]) -> set[str]:
    """Normalize a list of PEP 508 requirement strings to canonical package names.

    Skips blank lines / comments and tolerates direct git references (``pkg @
    git+https://…``), which ``packaging.requirements.Requirement`` parses fine.
    """
    names: set[str] = set()
    for raw in specifiers:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip trailing inline comments (`foo>=1  # why`) — not part of the spec.
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            names.add(canonicalize_name(Requirement(line).name))
        except InvalidRequirement:
            # Non-requirement lines (e.g. `-e .`, `-r other.txt`) aren't package
            # specs — ignore them; this guard is about named-package coverage.
            continue
    return names


def _pyproject_core_deps() -> set[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    return _canonical_names(deps) - _FULL_TIER_ONLY


def _requirements_core_deps() -> set[str]:
    lines = _REQUIREMENTS_CORE.read_text(encoding="utf-8").splitlines()
    return _canonical_names(lines)


def test_requirements_core_covers_pyproject_core_dependencies() -> None:
    """Every core ``[project.dependencies]`` package must be in requirements-core.txt.

    A package present in pyproject but absent here means the Docker image (which
    installs from requirements-core.txt) ships without it — the silent prod gap.
    """
    pyproject = _pyproject_core_deps()
    req_core = _requirements_core_deps()
    missing = pyproject - req_core
    assert not missing, (
        "requirements-core.txt is missing core deps declared in pyproject.toml "
        f"[project.dependencies]: {sorted(missing)}. Add them to requirements-core.txt "
        "(the Docker image installs from that file) — or, if one is genuinely "
        "full-tier-only, add it to _FULL_TIER_ONLY with a reason."
    )


def test_requirements_core_has_no_unknown_named_packages() -> None:
    """Belt-and-suspenders: requirements-core.txt declares no NAMED package that
    isn't in pyproject's core deps.

    Keeps the mirror tight in both directions — a stray pin in requirements-core.txt
    that no longer exists in the source of truth surfaces here rather than lingering.
    (pyproject is authoritative; this catches the reverse drift.)
    """
    pyproject = _pyproject_core_deps()
    req_core = _requirements_core_deps()
    extra = req_core - pyproject - _FULL_TIER_ONLY
    assert not extra, (
        "requirements-core.txt declares package(s) not in pyproject.toml "
        f"[project.dependencies]: {sorted(extra)}. pyproject is the source of truth — "
        "add them there too, or remove the stale line from requirements-core.txt."
    )


def test_parser_skips_non_package_lines() -> None:
    """The name extractor ignores comments, blanks, and pip directives (`-e .`)."""
    assert _canonical_names(["", "# comment", "-e .", "-r other.txt"]) == set()
    # canonicalize_name (PEP 503): lowercase + collapse runs of -_. to a single '-'.
    assert _canonical_names(["Foo.Bar_Baz>=1.0  # inline"]) == {"foo-bar-baz"}
    assert _canonical_names(["pkg @ git+https://example.com/pkg.git@v1"]) == {"pkg"}


# Guard the floor: tomllib is stdlib on 3.11+, which is requires-python. If this
# ever runs on an older interpreter the import above would already have failed —
# this assertion documents the expectation explicitly.
def test_running_on_supported_python() -> None:
    assert sys.version_info >= (3, 11), "requires-python is >=3.11 (tomllib is stdlib there)"
