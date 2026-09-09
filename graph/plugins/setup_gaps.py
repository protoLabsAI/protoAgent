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

import re
import threading

_LOCK = threading.Lock()
MAX_MESSAGE_CHARS = 300
MAX_GAPS_PER_PLUGIN = 16
# (plugin_id, key) -> {"plugin": plugin_id, "label": display name, "key": key, "message": text,
#                      optional "actions": [sanitized declarative action, …]}
_GAPS: dict[tuple[str, str], dict] = {}

# ── Declarative remediation actions (foundation for a future console mapper) ──────────────
# A gap MAY carry one or more *actions* — a bounded, declarative hint the console can later
# map to a "fix this" affordance ("Open plugin settings"). They are deliberately CLOSED,
# server-validated DATA, never behavior: a fixed ``kind`` vocabulary, bounded plain-text
# fields, no callback, no arbitrary URL, no markup. The host validates on the way IN and the
# console maps ``kind`` → a known UI affordance on the way out; a plugin string is never
# turned into a URL, HTML, or a callback. Anything not on the allowlist is dropped, and any
# malformed / oversized payload degrades to "no action" rather than raising.
ACTION_KINDS = ("plugin_config", "global_settings")
MAX_ACTIONS = 4  # a gap offering more than a handful of fixes is a bug, not a banner
MAX_ACTION_STR_CHARS = 120
MAX_ACTION_FIELDS = 8
# A config/settings target is an IDENTIFIER (a section slug / dotted path), never a URL:
# the char class excludes ``:`` so no ``scheme://`` can survive, and ``//`` is rejected too.
_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,119}$")


def _scrub(value) -> str:
    """A bounded plain-text fragment: collapse whitespace, strip the angle brackets that
    would let markup ride through, and cap the length. Never raises."""
    text = re.sub(r"\s+", " ", re.sub(r"[<>]", "", str(value))).strip()
    if len(text) > MAX_ACTION_STR_CHARS:
        text = text[: MAX_ACTION_STR_CHARS - 1] + "…"
    return text


def _sanitize_action(action, plugin_id: str) -> dict | None:
    """Project one caller-supplied action onto the closed, safe schema, or ``None`` if it
    can't be. Only allowlisted keys are ever copied, so a callback/URL/HTML field a plugin
    slips in is dropped by construction and the result is plain, serializable data."""
    if not isinstance(action, dict):
        return None
    kind = action.get("kind")
    if not isinstance(kind, str) or kind not in ACTION_KINDS:
        return None  # unknown/executable action kinds are never retained
    out: dict = {"kind": kind}
    # target — scoped and safe. ``plugin_config`` always points at the REPORTING plugin's
    # own section (a plugin can't aim the fix at another plugin, mirroring navigate()'s
    # scoping); a ``global_settings`` target must be a bounded settings-section identifier.
    if kind == "plugin_config":
        out["target"] = plugin_id
    else:  # global_settings — a reserved, safe global target
        target = action.get("target")
        if isinstance(target, str):
            target = target.strip()
            if target and "//" not in target and _TARGET_RE.match(target):
                out["target"] = target
    # label — optional display text; a string only, bounded, markup stripped.
    label = action.get("label")
    if isinstance(label, str):
        label = _scrub(label)
        if label:
            out["label"] = label
    # fields — optional config keys to highlight; bounded in count and length, non-strings
    # dropped.
    fields = action.get("fields")
    if isinstance(fields, (list, tuple)):
        clean = [f for f in (_scrub(x) for x in fields if isinstance(x, str)) if f][:MAX_ACTION_FIELDS]
        if clean:
            out["fields"] = clean
    return out


def _sanitize_actions(action, plugin_id: str) -> list[dict]:
    """Normalize an ``action`` argument (a single action, a list, or ``None``) into a
    bounded list of safe actions. Any bad entry is skipped; the whole thing never raises."""
    if action is None:
        return []
    raw = list(action) if isinstance(action, (list, tuple)) else [action]
    out: list[dict] = []
    for candidate in raw[:MAX_ACTIONS]:
        try:
            safe = _sanitize_action(candidate, plugin_id)
        except Exception:  # noqa: BLE001 — a hostile payload must degrade, not crash loading
            safe = None
        if safe:
            out.append(safe)
    return out


def _copy_gap(gap: dict) -> dict:
    """A defensive copy of a stored gap, including its nested actions, so a caller can't
    mutate the process-wide state through the returned view."""
    out = dict(gap)
    actions = out.get("actions")
    if actions is not None:
        out["actions"] = [{**a, "fields": list(a["fields"])} if "fields" in a else dict(a) for a in actions]
    return out


def report(plugin_id: str, key: str, message: str | None, *, label: str | None = None, action=None) -> None:
    """Set (``message``) or clear (``message=None`` / blank) one gap for a plugin.
    ``label`` is the plugin's display name for the banner; falls back to the id.

    ``action`` (optional) attaches a bounded, declarative remediation hint — a single
    action dict or a list — to the ACTIVE gap record. It is sanitized against the closed
    ``ACTION_KINDS`` vocabulary; anything unrecognized, oversized, or unsafe is silently
    dropped (see ``_sanitize_action``). Ignored when the gap is being cleared."""
    pid = str(plugin_id or "").strip()
    k = str(key or "").strip()
    if not pid or not k:
        return
    text = str(message).strip() if message is not None else ""
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    actions = _sanitize_actions(action, pid) if text else []
    with _LOCK:
        if text:
            if (pid, k) not in _GAPS and sum(1 for kk in _GAPS if kk[0] == pid) >= MAX_GAPS_PER_PLUGIN:
                return  # a plugin keying gaps by timestamp must not flood the banner strip
            record = {"plugin": pid, "label": (label or pid).strip() or pid, "key": k, "message": text}
            if actions:  # only present when at least one action survived — legacy calls store as before
                record["actions"] = actions
            _GAPS[(pid, k)] = record
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
        return [_copy_gap(v) for _, v in sorted(_GAPS.items())]


def warnings() -> list[str]:
    """The operator-facing banner lines: ``"<Plugin>: <message>"``."""
    return [f"{g['label']}: {g['message']}" for g in active()]


def reset() -> None:
    """Test hook — forget everything."""
    with _LOCK:
        _GAPS.clear()
