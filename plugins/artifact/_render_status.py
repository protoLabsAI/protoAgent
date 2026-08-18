"""Render feedback (#1458): the browser reports render ok/error AFTER the tool returns."""

from __future__ import annotations

import logging
import time

from . import _store

log = logging.getLogger("protoagent.plugins.artifact")

# ── Render feedback (#1458) ──────────────────────────────────────────────────
# The render happens ASYNC in the browser sandbox, AFTER the tool returns. The shell
# relays the sandbox's render result (ok / error) to POST /render-status, which stamps it
# onto the version. So show_/update_/rewrite_artifact can wait BRIEFLY for that result and
# report a render failure inline, closing the agent's code→render→fix loop. The wait only
# kicks in when a renderer is actually live (the panel polled recently) — headless / closed
# panel returns instantly, and the agent can still pull status later via check_artifact.
_LAST_POLL_TS = 0  # ms of the last panel poll (/history or /current); 0 = never seen a renderer
_RENDER_ERR_MAX = 2000  # cap a render-error string so a noisy stack can't bloat the store
_RENDER_ACTIVE_MS = 4000  # a poll within this window ⇒ a renderer is live and will report back
_RENDER_WAIT_MS = 3200  # max wait for a render result (≥ the sandbox's 3s no-mount guard)
_RENDER_POLL_MS = 120  # how often the wait re-reads the store


def _note_poll() -> None:
    global _LAST_POLL_TS
    _LAST_POLL_TS = _store._now()


def _renderer_live() -> bool:
    """True when the panel polled recently — i.e. a sandbox is mounted and WILL render the
    new version and report its result. Gates the inline wait so headless never blocks."""
    return _LAST_POLL_TS > 0 and (_store._now() - _LAST_POLL_TS) <= _RENDER_ACTIVE_MS


def _version_render(art: dict, version: int) -> dict | None:
    """The stored render result for 1-based ``version`` of ``art`` (or None)."""
    vers = art.get("versions") or []
    if 1 <= version <= len(vers):
        r = vers[version - 1].get("render")
        return r if isinstance(r, dict) else None
    return None


def _await_render(art_id: str, version: int) -> dict | None:
    """Block up to ``_RENDER_WAIT_MS`` for the sandbox to report version ``version``'s render
    result — but ONLY when a renderer is live, else return immediately. Checks the store
    BEFORE each sleep, so an already-recorded result returns instantly. Runs in the tool's
    worker thread, so the short sleep is safe (it doesn't block the event loop)."""
    if not art_id or not _renderer_live():
        return None
    deadline = _store._now() + _RENDER_WAIT_MS
    while True:
        r = _version_render(_store._find(_store._read_store(), art_id), version)
        if r is not None:
            return r
        if _store._now() >= deadline:
            return None
        time.sleep(_RENDER_POLL_MS / 1000)


def _render_suffix(art_id: str, version: int) -> str:
    """The inline render verdict appended to a create/edit reply, or '' when unknown."""
    r = _await_render(art_id, version)
    if r is None:
        return ""
    if r.get("ok"):
        return " It rendered cleanly."
    err = str(r.get("error") or "render failed").strip()
    return (
        f"\n\n⚠ But it FAILED to render:\n  {err}\n"
        "Fix it with update_artifact / rewrite_artifact (the artifact still exists; "
        "this error is advisory)."
    )
