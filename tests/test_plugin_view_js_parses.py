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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(("src", "attrs", "body"), _BLOCKS, ids=[b[0] for b in _BLOCKS])
def test_inline_view_js_parses(src: str, attrs: str, body: str):
    input_type = "module" if "module" in attrs else "commonjs"
    proc = subprocess.run(
        [NODE, "--input-type", input_type, "--check"],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"{src}: the browser would refuse this script —\n{proc.stderr.decode()[:800]}"
    )
