"""Every OS-touching test carries ``@pytest.mark.platform_sensitive``.

The Windows-native CI lane runs only ``-m platform_sensitive`` on PRs and main pushes
(a nightly scheduled run still covers the whole suite). That subset is only as good as
its marks, so this guard derives the obligation from the code instead of trusting
memory. A test must be marked (itself, its class, or the module's ``pytestmark``) when:

- its body, or a module-level helper/fixture it names, makes a real OS-sensitive call:
  spawning a process, signals, file locks, chmod/symlink, ``os.replace``/``os.rename``,
  ``shutil.rmtree``, or reads ``sys.platform``/``os.name``; or
- its module imports a first-party module that branches on the platform — that code
  has Windows-only paths the Linux job never takes (whole module must be marked).

Mocks don't count: ``monkeypatch.setattr(x.subprocess, "run", fake)`` is an attribute
reference, not a call. Fix a failure by adding the mark — or, for a module that merely
*imports* a platform-branching hub without exercising its OS code, add that hub to
``_HUB_MODULES`` with a reason.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = "platform_sensitive"

_CALLS: dict[str, set[str] | None] = {
    "subprocess": {"run", "Popen", "check_output", "check_call", "call"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "os": {"kill", "killpg", "chmod", "symlink", "replace", "rename", "link", "setsid", "getpgid", "fork", "startfile"},
    "shutil": {"rmtree", "move"},
    "signal": None,
    "fcntl": None,
    "msvcrt": None,
    "pty": None,
    "termios": None,
    "psutil": None,
}
# Path methods only — `.replace()`/`.rename()` are indistinguishable from str methods.
_PATH_METHODS = {"symlink_to", "chmod", "hardlink_to"}
_PLATFORM_ATTRS = {("sys", "platform"), ("os", "name")}
_FIRST_PARTY = (
    "a2a_impl", "events", "graph", "infra", "knowledge", "observability", "operator_api",
    "plugins", "runtime", "scheduler", "scripts", "security", "server", "tools",
)  # fmt: skip
# Platform-branching modules imported for unrelated reasons by most of the suite —
# importing one says nothing about exercising its OS-specific code.
_HUB_MODULES = {
    "infra.paths",  # home-dir resolution; imported by nearly every store
    "infra.clock",
    "graph.settings_schema",
    "graph.fleet.supervisor",  # one Windows branch in a large HTTP/pairing module
    "graph.providers.oauth",
}


def _dotted(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id, *reversed(parts)]
    return None


def _touches_os(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            d = _dotted(n.func)
            if d and len(d) >= 2 and d[-2] in _CALLS and (_CALLS[d[-2]] is None or d[-1] in _CALLS[d[-2]]):
                return True
            if isinstance(n.func, ast.Attribute) and n.func.attr in _PATH_METHODS:
                return True
            if d in (["FileLock"], ["filelock", "FileLock"]):
                return True
        if isinstance(n, ast.Attribute):
            d = _dotted(n)
            if d and tuple(d[-2:]) in _PLATFORM_ATTRS:
                return True
    return False


def _is_mark(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == MARK for n in ast.walk(node))


@cache
def _platform_modules() -> frozenset[str]:
    out = set()
    for top in _FIRST_PARTY:
        for path in (ROOT / top).rglob("*.py"):
            if "node_modules" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            if any(
                isinstance(n, ast.Attribute) and tuple(_dotted(n) or ())[-2:] in _PLATFORM_ATTRS for n in ast.walk(tree)
            ):
                out.add(".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__"))
    return frozenset(out - _HUB_MODULES)


def _imports(tree: ast.Module) -> set[str]:
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            mods |= {n.module, *(f"{n.module}.{a.name}" for a in n.names)}
        elif isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
    return mods


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_marked = any(
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets) and _is_mark(n.value)
        for n in tree.body
    )
    if module_marked:
        return []
    rel = path.relative_to(ROOT).as_posix()
    hits = sorted(_imports(tree) & _platform_modules())
    if hits:
        return [f"{rel}: imports platform-branching {', '.join(hits)} — add `pytestmark = pytest.mark.{MARK}`"]

    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    os_helpers = {name for name, n in funcs.items() if not name.startswith("test") and _touches_os(n)}
    marked_classes = {
        m.name
        for c in ast.walk(tree)
        if isinstance(c, ast.ClassDef) and any(_is_mark(d) for d in c.decorator_list)
        for m in c.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    out = []
    for name, fn in funcs.items():
        if not name.startswith("test") or name in marked_classes or any(_is_mark(d) for d in fn.decorator_list):
            continue
        used = {x.id for x in ast.walk(fn) if isinstance(x, ast.Name)} | {a.arg for a in fn.args.args}
        if _touches_os(fn) or used & os_helpers:
            out.append(f"{rel}::{name} — add `@pytest.mark.{MARK}`")
    return out


def test_every_os_touching_test_is_marked_platform_sensitive():
    problems = [v for p in sorted((ROOT / "tests").rglob("test_*.py")) if "integration" not in p.parts for v in _violations(p)]
    assert not problems, "the Windows CI lane would silently skip these:\n" + "\n".join(problems)


def test_the_guard_recognises_real_calls_and_ignores_mocks_and_docstrings():
    touching = ast.parse("def test_a(tmp_path):\n    import subprocess\n    subprocess.run(['git', 'init'])\n")
    mocked = ast.parse(
        'def test_b(monkeypatch):\n    """subprocess.run is faked"""\n    monkeypatch.setattr(m.subprocess, "run", fake)\n'
    )
    string_replace = ast.parse("def test_c():\n    assert 'a'.replace('a', 'b') == 'b'\n")
    assert _touches_os(touching)
    assert not _touches_os(mocked)
    assert not _touches_os(string_replace)
    assert "infra.proc" in _platform_modules()  # the guard's import rule still has a target
