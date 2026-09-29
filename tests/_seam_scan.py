"""Shared suite scanner for the extraction seam guards (``tests/test_*_seam.py``).

Each seam guard pins that no test patches a moved name on the module it moved OUT of —
there it is a copy of the binding, so the patch intercepts nothing. This module is the
one implementation of that scan (#3856), so every guard sees the same files and the same
target spellings:

* **Files:** every ``test_*.py`` under ``tests/`` AND under any ``tests*`` directory in
  ``plugins/`` (first-party plugin suites patch the host too).
* **Targets** — a patch/setattr/delattr call (``monkeypatch.setattr``, ``mock.patch``,
  ``patch.object``, ``setattr``, ``delattr``) whose target is the shim module, spelled as:
  the dotted string ``"pkg.mod.<name>"``; an alias (``import pkg.mod as m`` /
  ``from pkg import mod``); the attribute path itself (``pkg.mod`` after a bare
  ``import pkg.mod``); ``importlib.import_module("pkg.mod")`` or a local helper returning
  it; or ``sys.modules["pkg.mod"]``.

``state`` names are flagged on ANY attribute read through the shim (``chat._REG.clear()``):
module-level mutable state touched through a copy is a second registry.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Collection, Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_PATCH_CALLS = frozenset({"setattr", "object", "patch", "delattr"})


class Hit(str):
    """``file.py:LINE module.name`` — a str (joins into messages) carrying its parts."""

    path: Path
    lineno: int
    name: str

    def __new__(cls, path: Path, lineno: int, name: str) -> Hit:
        hit = super().__new__(cls, f"{path.name}:{lineno} {name}")
        hit.path, hit.lineno, hit.name = path, lineno, name
        return hit


def suite_files(repo: Path = REPO) -> list[Path]:
    """``tests/**/test_*.py`` plus ``plugins/**/tests*/**/test_*.py``."""
    files = set((repo / "tests").rglob("test_*.py"))
    plugins = repo / "plugins"
    if plugins.is_dir():
        for path in plugins.rglob("test_*.py"):
            parts = path.relative_to(plugins).parts[:-1]
            if "node_modules" not in parts and any(p.startswith("tests") for p in parts):
                files.add(path)
    return sorted(files)


def _dotted(node: ast.AST) -> str | None:
    """``a.b.c`` for a pure Name/Attribute chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _package_attr_is_module(pkg: str, leaf: str, dotted: str) -> bool:
    """``from pkg import leaf`` binds the MODULE (not e.g. ``server.chat``, the function
    ``server`` re-exports under its submodule's name)."""
    try:
        return getattr(importlib.import_module(pkg), leaf, None) is importlib.import_module(dotted)
    except Exception:  # noqa: BLE001 — unimportable here → assume the plain module binding
        return True


class _ModuleRefs:
    """Every spelling of ``dotted`` (the module object) in one parsed test file."""

    def __init__(self, tree: ast.AST, dotted: str) -> None:
        self.dotted = dotted
        self.helpers: set[str] = {
            fn.name
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(r, ast.Return) and r.value is not None and self._is_import(r.value) for r in ast.walk(fn)
            )
        }
        pkg, _, leaf = dotted.rpartition(".")
        self.aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == dotted and (a.asname or "." not in dotted):
                        self.aliases.add(a.asname or a.name)
            elif isinstance(node, ast.ImportFrom) and pkg and node.module == pkg and node.level == 0:
                for a in node.names:
                    if a.name == leaf and _package_attr_is_module(pkg, leaf, dotted):
                        self.aliases.add(a.asname or a.name)
            elif isinstance(node, ast.Assign) and self._is_import(node.value):
                self.aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    def _is_import(self, node: ast.AST) -> bool:
        """``importlib.import_module(dotted)``, ``sys.modules[dotted]`` or a local helper call."""
        if isinstance(node, ast.Subscript):
            return _dotted(node.value) == "sys.modules" and _const_str(node.slice) == self.dotted
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "import_module":
            return bool(node.args) and _const_str(node.args[0]) == self.dotted
        return name in getattr(self, "helpers", ())

    def __contains__(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name) and node.id in self.aliases:
            return True
        if isinstance(node, ast.Attribute) and _dotted(node) == self.dotted:
            return True
        return self._is_import(node)


def stale_patches(
    module: str,
    names: Collection[str] | None,
    *,
    state: Collection[str] = (),
    exclude: Iterable[Path] = (),
    files: Iterable[Path] | None = None,
) -> list[Hit]:
    """Patches of ``names`` (None: any name) on ``module``, plus ``state`` touches through it."""
    skip = {Path(p).resolve() for p in exclude}
    hits: list[Hit] = []
    for path in suite_files() if files is None else files:
        if path.resolve() in skip:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        refs = _ModuleRefs(tree, module)
        for node in ast.walk(tree):
            if state and isinstance(node, ast.Attribute) and node.attr in state and node.value in refs:
                hits.append(Hit(path, node.lineno, f"{module}.{node.attr}"))
                continue
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            if (func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")) not in _PATCH_CALLS:
                continue
            first = node.args[0]
            target = _const_str(first)
            if target is not None:
                owner, _, attr = target.rpartition(".")
                if owner == module and (names is None or attr in names):
                    hits.append(Hit(path, node.lineno, target))
            elif first in refs:
                attr = _const_str(node.args[1]) if len(node.args) > 1 else None
                if names is None or attr in names:
                    hits.append(Hit(path, node.lineno, f"{module}.{attr or '?'}"))
    return hits
