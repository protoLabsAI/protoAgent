"""A plugin tool module — the case that breaks bare ``import tools``: a MODULE-LEVEL
relative import (``from . import engine``) plus the ``@tool`` decorator. Only loadable
once the plugin is imported as a package (what the harness does)."""

from __future__ import annotations

from langchain_core.tools import tool

from . import engine


@tool
def summarize(items_json: str) -> str:
    """Classify items and report the counts (a stand-in plugin tool)."""
    import json

    out = engine.classify(json.loads(items_json))
    return f"{len(out['big'])} big, {len(out['small'])} small"
