r"""The shell page (ADR 0026 iframe) — a thin ``shell.html`` + the real ``shell.js``.

The page carries only the pre-parse inline script (slug-aware base + the kit CSS
link), the layout styles, and the markup; all shell logic lives in ``shell.js``,
loaded same-origin as a module (``src`` resolves against ``/plugins/artifact/view``).
Real files: editable and diffable as HTML/JS, no Python-string escaping — a literal
``</script>`` can no longer truncate the page, and the srcdoc strings the JS builds
keep their own ``<\/script>`` escapes (guard-tested). Both are read ONCE at import;
the frozen desktop build ships them automatically (build_sidecar bundles the whole
``plugins/`` tree as data).
"""

from __future__ import annotations

from pathlib import Path

_SHELL_HTML = (Path(__file__).parent / "shell.html").read_text(encoding="utf-8")
_SHELL_JS = (Path(__file__).parent / "shell.js").read_text(encoding="utf-8")
