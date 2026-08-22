"""Plugin-reported SETUP GAPS — the seam a plugin uses to tell the operator "I'm
installed and enabled but I cannot do my job until you do X" (missing binary, no
coder delegate, unauthenticated CLI, unbound repo).

Why a seam and not a log line: the console's operator status (``GET
/api/runtime/status`` → ``warnings[]``) is the ONE place an operator looks when an
agent seems broken, and before this every plugin preflight failure lived only in
``agent.log`` — a fresh Project Manager member booted with ``warnings: []`` while its
board threw a traceback every tick (the 2026-08-22 fresh-setup audit). A gap reported
here shows up as a banner; clearing it (``message=None``) removes the banner live, so
"install br / add a delegate / gh auth login" self-heals without a restart.

Plugins reach it through ``registry.report_setup_gap(key, message)`` (see
``PluginRegistry``); a plugin that must also run on older hosts guards with
``getattr(registry, "report_setup_gap", None)``. Process-wide, thread-safe, and
deliberately tiny: no history, no severity — a gap is either active or it isn't.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
MAX_MESSAGE_CHARS = 300
MAX_GAPS_PER_PLUGIN = 16
# (plugin_id, key) -> {"plugin": plugin_id, "label": display name, "key": key, "message": text}
_GAPS: dict[tuple[str, str], dict] = {}


def report(plugin_id: str, key: str, message: str | None, *, label: str | None = None) -> None:
    """Set (``message``) or clear (``message=None`` / blank) one gap for a plugin.
    ``label`` is the plugin's display name for the banner; falls back to the id."""
    pid = str(plugin_id or "").strip()
    k = str(key or "").strip()
    if not pid or not k:
        return
    text = str(message).strip() if message is not None else ""
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    with _LOCK:
        if text:
            if (pid, k) not in _GAPS and sum(1 for kk in _GAPS if kk[0] == pid) >= MAX_GAPS_PER_PLUGIN:
                return  # a plugin keying gaps by timestamp must not flood the banner strip
            _GAPS[(pid, k)] = {"plugin": pid, "label": (label or pid).strip() or pid, "key": k, "message": text}
        else:
            _GAPS.pop((pid, k), None)


def clear_plugin(plugin_id: str) -> None:
    """Drop every gap a plugin reported — used when a plugin is unloaded/disabled so a
    stale banner can't outlive the plugin that raised it."""
    pid = str(plugin_id or "").strip()
    with _LOCK:
        for k in [k for k in _GAPS if k[0] == pid]:
            _GAPS.pop(k, None)


def retain(plugin_ids: set[str] | list[str]) -> None:
    """Drop gaps from plugins that are no longer present at all (uninstalled between
    reloads) — the disabled-branch clear can't see a plugin the loader never visits."""
    keep = {str(p) for p in plugin_ids}
    with _LOCK:
        for k in [k for k in _GAPS if k[0] not in keep]:
            _GAPS.pop(k, None)


def active() -> list[dict]:
    """Every active gap, stable order (plugin, key)."""
    with _LOCK:
        return [dict(v) for _, v in sorted(_GAPS.items())]


def warnings() -> list[str]:
    """The operator-facing banner lines: ``"<Plugin>: <message>"``."""
    return [f"{g['label']}: {g['message']}" for g in active()]


def reset() -> None:
    """Test hook — forget everything."""
    with _LOCK:
        _GAPS.clear()
