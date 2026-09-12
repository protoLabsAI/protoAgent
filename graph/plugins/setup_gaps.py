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

# -- Declarative remediation actions (foundation for a future console mapper) --
# A gap MAY carry one or more *actions* — a bounded, declarative hint the console can later
# map to a "fix this" affordance ("Open plugin settings"). They are deliberately CLOSED,
# server-validated DATA, never behavior: a fixed ``kind`` vocabulary, bounded plain-text
# fields, no callback, no arbitrary URL, no markup. The host validates on the way IN and the
# console maps ``kind`` → a known UI affordance on the way out; a plugin string is never
# turned into a URL, HTML, or a callback. Anything not on the allowlist is dropped, and any
# malformed / oversized payload degrades to "no action" rather than raising.
#
# ``plugin_setup`` is the one kind whose button DOES something server-side, and it stays data
# all the same: the action names a ``step`` — an identifier — that the REPORTING plugin
# registered at load time with ``registry.register_setup_step(step, fn)``. The console POSTs
# ``/api/plugins/<gap.plugin>/setup-steps/<step>`` and the host runs the callable it holds for
# exactly that (plugin, step) pair; nothing in the action is ever executed, fetched, or turned
# into a URL, and a step another plugin registered is unreachable through it (the target is
# forced to the reporting plugin, like ``plugin_config``). It exists for fixes that are a
# COMMAND, not a setting — "download the CLI", "install Chrome" — which the operator would
# otherwise be told to go and run in a terminal.
ACTION_KINDS = ("plugin_config", "global_settings", "plugin_setup")
MAX_ACTIONS = 4  # a gap offering more than a handful of fixes is a bug, not a banner
MAX_ACTION_STR_CHARS = 120
MAX_ACTION_FIELDS = 8
# A config/settings target is an IDENTIFIER (a section slug / dotted path), never a URL:
# the char class excludes ``:`` so no ``scheme://`` can survive, and ``//`` is rejected too.
_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,119}$")
_URLISH_RE = re.compile(r"(^|[^\w])(?:[A-Za-z][A-Za-z0-9+.-]*:|//)")
# A setup-step id: one lowercase path segment (it lands in the console's POST path), so no
# ``/``, no ``.``, no ``:`` — nothing that could reshape the URL it is placed into.
_STEP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_STEPS_PER_PLUGIN = 8
# (plugin_id, step) -> the plugin's callable. SERVER-SIDE ONLY: never serialized, never in a
# gap record — the gap carries the step's NAME, the host keeps the behavior.
_STEPS: dict[tuple[str, str], object] = {}


def _safe_text(value: str) -> str | None:
    """Project a caller-supplied string onto the safe display-text subset.

    HTML-ish and URL-ish values are omitted instead of transformed into retained text:
    the future console mapper owns navigation/actions, and plugin-provided strings never
    become markup, links, or callbacks. Oversized values are also omitted; callers may
    retry with a concise label/field instead of storing a truncated surprise.
    """
    if "<" in value or ">" in value or _URLISH_RE.search(value):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text or len(text) > MAX_ACTION_STR_CHARS:
        return None
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
    elif kind == "plugin_setup":
        # A run-this action with nothing (valid) to run is no action at all — dropped, not
        # stored with a blank step the console would POST. The step is an identifier the
        # reporting plugin registered; the target is forced to that plugin.
        step = action.get("step")
        step = step.strip() if isinstance(step, str) else ""
        if not _STEP_RE.match(step):
            return None
        out["target"] = plugin_id
        out["step"] = step
    else:  # global_settings — a reserved, safe global target
        target = action.get("target")
        if isinstance(target, str):
            target = target.strip()
            if target and "//" not in target and _TARGET_RE.match(target):
                out["target"] = target
    # label — optional display text; a string only, bounded, no markup/URLs.
    label = action.get("label")
    if isinstance(label, str):
        label = _safe_text(label)
        if label:
            out["label"] = label
    # fields — optional config keys to highlight; bounded in count and length, non-strings
    # dropped.
    fields = action.get("fields")
    if isinstance(fields, (list, tuple)):
        clean = []
        for candidate in fields:
            if len(clean) >= MAX_ACTION_FIELDS:
                break
            if not isinstance(candidate, str):
                continue
            field = _safe_text(candidate)
            if field:
                clean.append(field)
        if clean:
            out["fields"] = clean
    return out


def _sanitize_actions(action, plugin_id: str) -> list[dict]:
    """Normalize an ``action`` argument (a single action, a list, or ``None``) into a
    bounded list of safe actions. Any bad entry is skipped; the whole thing never raises."""
    if action is None:
        return []
    raw = action if isinstance(action, (list, tuple)) else [action]
    out: list[dict] = []
    for index, candidate in enumerate(raw):
        if index >= MAX_ACTIONS:
            break
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
    stale banner can't outlive the plugin that raised it. Its setup steps go with it: a
    disabled plugin's code must not stay reachable through a stale banner button."""
    pid = str(plugin_id or "").strip()
    with _LOCK:
        for k in [k for k in _GAPS if k[0] == pid]:
            _GAPS.pop(k, None)
        for k in [k for k in _STEPS if k[0] == pid]:
            _STEPS.pop(k, None)


def retain(plugin_ids: set[str] | list[str]) -> None:
    """Drop gaps (and setup steps) from plugins that are no longer present at all
    (uninstalled between reloads) — the disabled-branch clear can't see a plugin the
    loader never visits."""
    keep = {str(p) for p in plugin_ids}
    with _LOCK:
        for k in [k for k in _GAPS if k[0] not in keep]:
            _GAPS.pop(k, None)
        for k in [k for k in _STEPS if k[0] not in keep]:
            _STEPS.pop(k, None)


# -- Setup STEPS: the server-side half of a ``plugin_setup`` action --------------------


def register_step(plugin_id: str, step: str, fn) -> bool:
    """Hold ``fn`` as the plugin's setup step ``step`` (what a ``plugin_setup`` banner
    button runs). Returns False — and holds nothing — for a bad id, a non-callable, or a
    plugin past ``MAX_STEPS_PER_PLUGIN``. Re-registering replaces (a reload re-registers)."""
    pid = str(plugin_id or "").strip()
    name = step.strip() if isinstance(step, str) else ""
    if not pid or not _STEP_RE.match(name) or not callable(fn):
        return False
    with _LOCK:
        if (pid, name) not in _STEPS and sum(1 for k in _STEPS if k[0] == pid) >= MAX_STEPS_PER_PLUGIN:
            return False
        _STEPS[(pid, name)] = fn
    return True


def has_step(plugin_id: str, step: str) -> bool:
    with _LOCK:
        return (str(plugin_id or "").strip(), str(step or "").strip()) in _STEPS


def run_step(plugin_id: str, step: str) -> dict | None:
    """Run a registered step and normalize what it says: ``{"ok", "message", "pending"}``,
    or ``None`` when no such step is registered. Blocking — callers run it off the event
    loop. A step is expected to START long work (a download, an install) in the background
    and return ``pending: True``; its gap then carries the progress, and clears when it's
    done. A step that raises is reported as ``ok: False`` with the error, never a 500.

    A step returns a message string (success), or a dict with any of ``ok`` / ``message``
    / ``pending``. Anything else is a success with no message."""
    key = (str(plugin_id or "").strip(), str(step or "").strip())
    with _LOCK:
        fn = _STEPS.get(key)
    if fn is None:
        return None
    try:
        raw = fn()
    except Exception as exc:  # noqa: BLE001 — a plugin bug is the operator's message, not a 500
        raw = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    if isinstance(raw, dict):
        ok, message, pending = raw.get("ok", True) is not False, raw.get("message"), bool(raw.get("pending"))
    else:
        ok, message, pending = True, raw, False
    text = re.sub(r"\s+", " ", str(message)).strip() if message is not None else ""
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    return {"ok": ok, "message": text, "pending": pending and ok}


def active() -> list[dict]:
    """Every active gap, stable order (plugin, key)."""
    with _LOCK:
        return [_copy_gap(v) for _, v in sorted(_GAPS.items())]


def warning_line(gap: dict) -> str:
    """The legacy operator-facing line for one gap: ``"<Plugin>: <message>"``. The console
    drops exactly this line from ``warnings[]`` when it renders the gap's record as a banner
    (``gapWarningLine`` in apps/web/src/app/SetupGapBanner.tsx), so the two must not drift."""
    return f"{gap['label']}: {gap['message']}"


def warnings(gaps: list[dict] | None = None) -> list[str]:
    """The operator-facing banner lines: ``"<Plugin>: <message>"``.

    Pass ``gaps`` — a snapshot from :func:`active` — when publishing the lines BESIDE those
    records (runtime status does): two separate reads can straddle a plugin re-reporting or
    clearing a gap on another thread, and a line whose record isn't in the same payload
    renders as a plain alert the operator can neither act on nor dismiss."""
    return [warning_line(g) for g in (active() if gaps is None else gaps)]


def reset() -> None:
    """Test hook — forget everything."""
    with _LOCK:
        _GAPS.clear()
        _STEPS.clear()
