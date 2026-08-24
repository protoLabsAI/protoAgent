"""The generated plugin API reference must match the code it documents.

The plugin system is the extension contract, and the failure mode this guards against is
specific: someone adds a ``register_*`` seam, a manifest field, or an SDK call, ships it,
and the reference quietly stops being complete. Nobody notices, because nothing breaks —
docs rot silently by construction.

So the reference is generated (``scripts/gen_plugin_api.py``) and this suite fails when the
committed pages drift, the same way ``test_docs_plugin.test_nav_json_in_sync_with_sidebar``
guards the docs nav. The remedy is always one command, printed in the failure message.

The coverage assertions below are the part that matters most: they say every *public*
symbol reaches the docs, so growing the API surface without documenting it is a red build.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "docs" / "reference"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_plugin_api_under_test", REPO / "scripts" / "gen_plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _generator()


def test_generated_pages_are_current(gen) -> None:
    """Every committed page matches what the generator produces from today's source."""
    stale = []
    for filename, builder in gen.PAGES.items():
        target = REFERENCE / filename
        assert target.exists(), f"{filename} is missing — run `python scripts/gen_plugin_api.py`"
        if target.read_text(encoding="utf-8") != builder():
            stale.append(filename)
    assert not stale, (
        f"Stale generated plugin reference: {', '.join(stale)}. "
        "Run `python scripts/gen_plugin_api.py` and commit the result."
    )


def test_every_registry_seam_is_documented(gen) -> None:
    """A new ``register_*`` seam must reach the reference, not just the code.

    This is the assertion the whole file exists for: plugins are extended by adding seams,
    and an undocumented seam is one nobody outside this repo can use.
    """
    from graph.plugins.registry import PluginRegistry

    page = (REFERENCE / "plugin-registry-api.md").read_text(encoding="utf-8")
    missing = [name for name, _ in gen._public_methods(PluginRegistry) if f"`registry.{name}`" not in page]
    assert not missing, f"Undocumented registry seams: {missing}. Run `python scripts/gen_plugin_api.py`."


def test_every_sdk_function_is_documented(gen) -> None:
    from graph import sdk

    page = (REFERENCE / "plugin-sdk-api.md").read_text(encoding="utf-8")
    missing = [name for name, _ in gen._public_functions(sdk) if f"`sdk.{name}`" not in page]
    assert not missing, f"Undocumented SDK calls: {missing}. Run `python scripts/gen_plugin_api.py`."


def test_every_manifest_field_is_documented(gen) -> None:
    from dataclasses import fields

    from graph.plugins.manifest import PluginManifest

    page = (REFERENCE / "plugin-manifest.md").read_text(encoding="utf-8")
    # `path` is resolved by the loader, never authored in YAML, so it isn't in the schema doc.
    missing = [f.name for f in fields(PluginManifest) if f.name != "path" and f"### `{f.name}`" not in page]
    assert not missing, f"Undocumented manifest fields: {missing}. Run `python scripts/gen_plugin_api.py`."


def test_no_symbol_ships_without_prose(gen) -> None:
    """Generation alone isn't documentation — a symbol with no docstring or comment renders
    an explicit placeholder, and that placeholder must never reach a committed page."""
    offenders = [
        filename for filename in gen.PAGES if gen.UNDOCUMENTED in (REFERENCE / filename).read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"These pages contain undocumented symbols: {offenders}. "
        "Add a docstring (or a comment above the field) in the source module, then regenerate."
    )


def test_docstring_rendering_is_interpreter_independent(gen) -> None:
    """The generated pages must be byte-identical on every supported Python.

    Python 3.13 strips docstring indentation at compile time; 3.12 does not. The generator
    originally used `textwrap.dedent`, which is a no-op on a docstring whose first line
    carries no indentation — so the same source produced differently-indented pages on
    3.12 and 3.13, and the staleness gate could never pass in CI (3.12) once a page was
    committed from a 3.13 machine. It cost a red build to find, so it gets a test.
    """
    as_312 = "First line.\n\n        Indented continuation, the way 3.12 hands it over.\n        Still indented.\n    "
    as_313 = "First line.\n\nIndented continuation, the way 3.12 hands it over.\nStill indented.\n"

    assert gen._clean_doc(as_312) == gen._clean_doc(as_313), (
        "docstring rendering depends on the interpreter's docstring indentation handling — "
        "use inspect.cleandoc, never textwrap.dedent, on a __doc__"
    )
    assert "\n        Indented" not in gen._clean_doc(as_312), "source indentation leaked into the page"


def test_signature_keeps_default_values_while_unquoting_annotations(gen) -> None:
    """`from __future__ import annotations` quotes every annotation, so the generator
    unquotes them — but a blanket regex over the rendered signature also strips the quotes
    off default VALUES, which turned `subdir: str = ''` into `subdir: str = ` in the docs.
    """

    # Bare annotations, like the real source. (An explicitly *quoted* annotation is
    # already a string in the source text, so `from __future__ import annotations` keeps
    # its quotes verbatim — a different case, and not one this codebase writes.)
    def sample(a: str = "", b: int = 3, *, c: dict | None = None) -> str:
        return ""

    rendered = gen._sig(sample)

    assert "a: str = ''" in rendered, f"empty-string default was eaten: {rendered}"
    assert "-> str" in rendered and "-> 'str'" not in rendered, f"annotation still quoted: {rendered}"
    assert "c: dict | None = None" in rendered, rendered


def test_fenced_examples_in_docstrings_survive_intact(gen) -> None:
    """A docstring may carry a runnable example. The RST ``double backtick`` → `single`
    rewrite ate two of a fence's three backticks, and linkification rewrote identifiers
    inside the sample, so the example rendered as prose.
    """
    doc = 'Summary line.\n\n```python\nx = f("ADR 0004")  # see #1234\n```\n'

    out = gen._clean_doc(doc)

    assert "```python" in out, f"fence was mangled: {out!r}"
    assert 'x = f("ADR 0004")  # see #1234' in out, f"sample was rewritten: {out!r}"


def test_event_scan_finds_the_topics_it_should(gen) -> None:
    """Guard the guard: the topic catalog is built by scanning for a fixed set of publisher
    call names, which is its blind spot. A new forwarding helper — `_publish` was one, and
    it hid every `watch.*` topic on the first run — means silently missing events, and the
    page would still look complete. These sentinels come from four different modules and
    three different publisher spellings.
    """
    topics = gen._scan_topics()
    for expected in ("turn.started", "watch.met", "ui.navigate", "inbox.item", "turn.usage"):
        assert expected in topics, (
            f"the bus-topic scan no longer finds {expected!r} — a publisher helper was probably "
            f"renamed; check gen_plugin_api._PUBLISHERS"
        )
    assert len(topics) >= 25, f"only {len(topics)} topics found — the scan is probably broken"
    assert topics["turn.usage"]["keys"], "payload keys stopped being extracted"


def test_event_catalog_documents_every_scanned_topic(gen) -> None:
    page = (REFERENCE / "plugin-events.md").read_text(encoding="utf-8")
    missing = [t for t in gen._scan_topics() if f"`{t}`" not in page]
    assert not missing, f"Topics missing from the catalog: {missing}. Run `python scripts/gen_plugin_api.py`."


def test_event_scan_emits_posix_paths_on_every_platform(gen) -> None:
    """Like the docstrings, the "Emitted from" paths must be byte-identical on every
    OS — Windows CI regenerates the page and compares it to the committed bytes, and
    a `str(Path)` backslash separator made `plugin-events.md` permanently stale there."""
    for topic, rec in gen._scan_topics().items():
        bad = [s for s in rec["sources"] if "\\" in s]
        assert not bad, f"non-POSIX source paths for {topic!r}: {bad} — use as_posix() in _scan_topics"
