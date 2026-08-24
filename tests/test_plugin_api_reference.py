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
