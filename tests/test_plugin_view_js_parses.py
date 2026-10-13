"""Inline JS in bundled plugin views must actually parse (#2471).

Plugin views ship as HTML-in-a-Python-string, so their JavaScript is never seen
by any bundler, linter, or test — the first parser to read it is the user's
browser. v0.130.0 shipped the Docs view dead on exactly that: a ``\\'`` written
for cooked-string semantics inside a RAW string reached the browser verbatim,
terminated the JS string literal, and the whole module failed with
``SyntaxError: Unexpected identifier 't'``.

This sweep extracts every module-level string constant that carries a
``<script>`` block from every ``plugins/**/*.py`` — via ``ast``, so raw and
cooked strings yield exactly the bytes the browser will receive — and runs each
script body through ``node --check``. Skips (with a reason) when node is absent;
CI runners all carry node.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.platform_sensitive

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

_SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.S | re.I)


def _script_blocks() -> list[tuple[str, str, str]]:
    """(source-id, attrs, body) for every inline script in every plugin-view string."""
    blocks: list[tuple[str, str, str]] = []
    for py in sorted((ROOT / "plugins").rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:  # a broken plugin file is another test's problem
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if "<script" not in node.value:
                continue
            for i, m in enumerate(_SCRIPT_RE.finditer(node.value)):
                attrs, body = m.group(1), m.group(2)
                # Only classic/module scripts hold JS — importmap/JSON/template
                # script tags carry other grammars (the notes view ships an
                # importmap, which is JSON).
                type_m = re.search(r"""type\s*=\s*["']([^"']+)["']""", attrs)
                script_type = (type_m.group(1) if type_m else "").lower()
                if script_type not in ("", "module", "text/javascript", "application/javascript"):
                    continue
                if body.strip():
                    # as_posix(): block ids must be separator-stable so the
                    # docs-view sanity probe below matches on Windows too.
                    rel = py.relative_to(ROOT).as_posix()
                    blocks.append((f"{rel}:{node.lineno}#{i}", attrs, body))
    return blocks


_BLOCKS = _script_blocks()


def test_sweep_found_the_docs_view():
    """If extraction ever finds nothing, the sweep is broken — not the views."""
    assert any("plugins/docs" in b[0] for b in _BLOCKS), _BLOCKS


def _run_node_check(src: str, attrs: str, body: str) -> subprocess.CompletedProcess[bytes]:
    """Parse one script, tolerating one transient Node process stall."""
    input_type = "module" if "module" in attrs else "commonjs"
    command = [NODE, "--input-type", input_type, "--check"]
    for attempt in range(2):
        try:
            return subprocess.run(
                command,
                input=body.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            if attempt == 1:
                raise AssertionError(f"{src}: node --check timed out twice (30 seconds per attempt)") from exc
    raise AssertionError("unreachable")


def test_node_check_does_not_retry_syntax_failure(monkeypatch):
    calls = 0

    def fail_syntax(*args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(args[0], 1, b"", b"SyntaxError")

    monkeypatch.setattr(subprocess, "run", fail_syntax)

    proc = _run_node_check("plugins/example.py:1#0", "", "not valid")

    assert proc.returncode == 1
    assert calls == 1


def test_node_check_retries_one_timeout(monkeypatch):
    calls = 0

    def timeout_then_pass(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return subprocess.CompletedProcess(args[0], 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", timeout_then_pass)

    proc = _run_node_check("plugins/example.py:1#0", "", "const ok = true;")

    assert proc.returncode == 0
    assert calls == 2


def test_node_check_fails_after_two_timeouts(monkeypatch):
    def always_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", always_timeout)

    with pytest.raises(AssertionError, match=r"plugins/example\.py:1#0.*timed out twice"):
        _run_node_check("plugins/example.py:1#0", "", "const stalled = true;")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(("src", "attrs", "body"), _BLOCKS, ids=[b[0] for b in _BLOCKS])
def test_inline_view_js_parses(src: str, attrs: str, body: str):
    proc = _run_node_check(src, attrs, body)
    assert proc.returncode == 0, f"{src}: the browser would refuse this script —\n{proc.stderr.decode()[:800]}"
