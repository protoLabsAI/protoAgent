"""Artifact plugin (ADR 0038) — generative UI on demand.

The agent calls ``show_artifact(kind, code)`` to render HTML / SVG / Mermaid / Markdown / React into the
console's Artifact panel, then iterates it with ``update_artifact`` (a targeted string-replace
edit) or ``rewrite_artifact`` (a full replacement) — the Claude "update vs rewrite" model, so an
artifact is a VERSION CHAIN you can step back through, not a flood of near-duplicates.
``list_artifacts`` / ``get_artifact`` (read the current source — how you take over an artifact you
didn't author) / ``delete_artifact`` manage them. ``save_file_artifact`` (ADR 0092) versions a
generated FILE (docx/xlsx/pptx/pdf/image) as a download artifact — bytes in a sidecar blob, a
diffable text preview in ``code``, an image thumbnail — rendered as a download card, not iframed.
The panel is a plugin-served shell page
(iframed by the console, ADR 0026) that renders the generated code in a **nested sandboxed
iframe** (``sandbox="allow-scripts"``, no same-origin) — the Claude Artifacts / Open WebUI
isolation model: generated code runs, but can't touch the console, its cookies, or its APIs.

State is persisted to a **file** (instance-scoped), not module memory — under the ACP runtime the
tool executes in the operator-MCP process while the route is served by the main process, so the
two only share state through disk.
"""

from __future__ import annotations

import logging

# Submodule map (the 2026-08 decomposition of the old 1,800-line monolith, #2817):
#   _config         config/env knob resolution (ENV > Settings UI > default)
#   _store          file-backed version chains + sidecar blobs + event emit
#   _preview        file previews: mime/clip/extractors/thumbnails
#   _render_status  browser render feedback (#1458)
#   _bundle         chat-bundle consumption seam (#2681)
#   _tools          the eight agent-facing tools + the full-body-write nudge
#   _routes         the public PAGE router + the gated DATA router
#   _shell          the console shell page as one static string
# Cross-module references are MODULE-QUALIFIED (``_store._now()``) so a test that
# monkeypatches the owning module's global patches every reader.
from ._bundle import resolve_for_bundle
from ._config import _ask_enabled, _max_history
from ._preview import _PREVIEW_TRUNC, _clip
from ._render_status import _RENDER_ERR_MAX, _render_suffix, _renderer_live
from ._routes import _VENDOR_FILES, _build_data_router, _build_view_router
from ._shell import _SHELL_HTML
from ._store import (
    _blob_path,
    _blob_root,
    _find,
    _gc_blobs,
    _now,
    _read_store,
    _store_path,
    _write_store,
)
from ._tools import (
    _SAVE_NUDGE_WINDOW_MS,
    check_artifact,
    delete_artifact,
    get_artifact,
    list_artifacts,
    rewrite_artifact,
    save_file_artifact,
    show_artifact,
    update_artifact,
)
from . import _store as _store_mod

log = logging.getLogger("protoagent.plugins.artifact")

__all__ = [
    "register",
    "resolve_for_bundle",
    "show_artifact",
    "save_file_artifact",
    "update_artifact",
    "rewrite_artifact",
    "list_artifacts",
    "get_artifact",
    "check_artifact",
    "delete_artifact",
]


def register(registry) -> None:
    _store_mod._REGISTRY = registry  # the event-emit seam lives with the store commits
    for t in (
        show_artifact,
        save_file_artifact,
        update_artifact,
        rewrite_artifact,
        list_artifacts,
        get_artifact,
        check_artifact,
        delete_artifact,
    ):
        registry.register_tool(t)
    registry.register_skill_dir(
        "skills"
    )  # teaches: render with show_artifact, edit with update/rewrite, don't write files
    # TWO routers at DISTINCT prefixes (a same-prefix second router is silently
    # de-duped by the host): the PAGE on public /plugins/artifact (iframe-loadable,
    # base-derivation-safe) and the DATA routes on gated /api/plugins/artifact.
    registry.register_router(_build_view_router(), prefix="/plugins/artifact")
    registry.register_router(_build_data_router(), prefix="/api/plugins/artifact")
