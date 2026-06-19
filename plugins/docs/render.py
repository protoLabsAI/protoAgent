"""Server-side markdown → HTML for the Docs reader view.

Rendered in the plugin (not the iframe) so the view ships no JS markdown bundle and works
offline / in the frozen desktop app — the `docs_read` tool still returns raw markdown for
the agent; only the view needs HTML. CommonMark + GFM tables (the docs are table-heavy);
no linkify (avoids an extra dep — the docs use explicit `[text](url)` links).
"""

from __future__ import annotations

from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def render_markdown(md: str) -> str:
    """Render markdown to an HTML fragment (the view injects it into a `.markdown` block)."""
    return _MD.render(md or "")
