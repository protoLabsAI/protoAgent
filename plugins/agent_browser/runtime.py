"""Shared runtime helpers — build the `agent-browser` global launch flags from config.

Used by BOTH the browser tools (`browser_open`) and the panel's nav route, so a session
launched from the console Start button gets the same headed / profile / device / anti-
detection setup as one the agent opens. Kept dependency-free so importing it never drags
in langchain (the panel imports it too).

Flags apply at **session launch** (the first `open`). A session already running without
them keeps its old setup until it's closed and reopened.

In-tree additions (#3451 review), kept here because the tools AND the panel need them and
this module is the one both import without dragging in langchain: ``number`` (a numeric
setting that tolerates a hand-written config) and ``bad_operand`` (the argv option guard).
The anti-detection code below is unchanged from the source repo.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("protoagent.plugins.agent_browser")

# A realistic desktop Chrome UA (no "HeadlessChrome" giveaway). Used for stealth when
# running headless and no explicit user_agent is set; override with `user_agent`.
#
# The Chrome version is the INSTALLED Chrome's major — the one doctor reports and that
# actually drives the page (the preflight probe notes it here) — in Chrome's own reduced-UA
# form, `Chrome/<major>.0.0.0`, which is what a real Chrome sends. A UA claiming a Chrome
# other than the one executing the page is a tell of its own (its JS surface and Client
# Hints give the real version away). Until a probe has seen Chrome — or if doctor can't say
# — it falls back to _FALLBACK_CHROME_MAJOR. Stealth stays OFF by default (the manifest).
_FALLBACK_CHROME_MAJOR = 149
_STEALTH_UA_TEMPLATE = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
_STEALTH_UA = _STEALTH_UA_TEMPLATE.format(major=_FALLBACK_CHROME_MAJOR)
_CHROME = {"major": 0}  # the detected installed-Chrome major; 0 = not detected yet


def note_chrome_version(version: str) -> int:
    """Remember the installed Chrome's version (``"149.0.7827.55"``) for the stealth UA.
    Returns the major it kept, or 0 — leaving any earlier detection in place — when the text
    isn't a plausible Chrome version."""
    m = re.match(r"^\s*(\d{2,4})\.\d+", str(version or ""))
    major = int(m.group(1)) if m else 0
    if not 60 <= major <= 999:
        return 0
    _CHROME["major"] = major
    return major


def stealth_user_agent() -> str:
    """The stealth UA for the detected Chrome, else the fallback major."""
    return _STEALTH_UA_TEMPLATE.format(major=_CHROME["major"] or _FALLBACK_CHROME_MAJOR)


def launch_flags(cfg: dict | None) -> list[str]:
    """Curated runtime knobs → `agent-browser` global flags. Blank/0/false → omitted
    (CLI default). Order is stable so tests can assert argv."""
    cfg = cfg or {}
    f: list[str] = []
    headed = bool(cfg.get("headed"))
    # Extra Chrome launch args (comma/newline separated); anti-detection + anti-throttle
    # flags get merged in below.
    args = [a.strip() for a in str(cfg.get("browser_args") or "").replace("\n", ",").split(",") if a.strip()]
    if headed:
        f.append("--headed")
        # A headed window gets throttled/paused when it loses focus or is occluded, which
        # stalls the live screencast. Keep it rendering so the panel updates even when the
        # operator is looking at another window.
        for a in ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                  "--disable-background-timer-throttling"):
            if a not in args:
                args.append(a)
    if str(cfg.get("profile") or "").strip():
        f += ["--profile", str(cfg["profile"]).strip()]
    if str(cfg.get("device") or "").strip():
        f += ["--device", str(cfg["device"]).strip()]
    if str(cfg.get("allowed_domains") or "").strip():
        f += ["--allowed-domains", str(cfg["allowed_domains"]).strip()]
    if str(cfg.get("confirm_actions") or "").strip():
        f += ["--confirm-actions", str(cfg["confirm_actions"]).strip()]
    max_output = number(cfg, "max_output", 0, cast=int)
    if max_output > 0:
        f += ["--max-output", str(max_output)]

    # ── anti-detection ──────────────────────────────────────────────────────────
    # `stealth` layers on the common evasions: drop the `navigator.webdriver` automation
    # flag, and (when headless, where the UA says "HeadlessChrome") swap in a real desktop UA.
    ua = str(cfg.get("user_agent") or "").strip()
    if bool(cfg.get("stealth")):
        if "--disable-blink-features=AutomationControlled" not in args:
            args.append("--disable-blink-features=AutomationControlled")
        if not ua and not headed:
            ua = stealth_user_agent()
    if ua:
        f += ["--user-agent", ua]
    if args:
        f += ["--args", ",".join(args)]
    return f


# ── numeric settings ─────────────────────────────────────────────────────────────


def number(cfg: dict | None, key: str, default, *, cast=float, positive: bool = False):
    """A numeric plugin setting, or ``default`` when it isn't one.

    ``type: number`` in the manifest validates edits made in Settings only; a hand-written
    ``langgraph-config.yaml`` reaches the plugin raw. So ``timeout_s: "1m"`` made
    ``float()`` raise inside ``get_browser_tools`` — ``register()`` caught it, and the agent
    silently had NO browser tools. Blank/None means "use the default"; ``positive`` also
    rejects 0 and negatives (a 0 s timeout would time every command out)."""
    raw = (cfg or {}).get(key, default)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError, OverflowError):
        log.warning("[agent_browser] ignoring %s=%r (not a number); using %r", key, raw, default)
        return default
    if positive and not value > 0:
        log.warning("[agent_browser] ignoring %s=%r (must be > 0); using %r", key, raw, default)
        return default
    return value


def flag(cfg: dict | None, key: str, default: bool) -> bool:
    """A boolean setting that tolerates a hand-written config: ``"false"`` / ``"0"`` /
    ``"off"`` are false (``bool("false")`` is True). ``type: bool`` validates Settings edits
    only. Blank/None means the default."""
    raw = (cfg or {}).get(key, default)
    if raw is None or raw == "":
        return default
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


# ── the argv option guard (the tools AND the panel's /nav route) ──────────────────
# The CLI's option grammar — `-h`, `--help`, `--headed`, … : a dash, optionally another,
# then a LETTER. See bad_operand for why this, and not "any leading dash".
OPTION_LIKE = re.compile(r"^--?[A-Za-z]")
WORKAROUND = {
    "text": ("To enter it anyway, set the field from JavaScript with browser_eval, e.g. "
             "document.querySelector('#q').value = '--foo' (then dispatch an 'input' event if "
             "the page reacts to typing)."),
    "key": "Key names never start that way; for the minus key pass '-' (or 'Minus').",
    "expression": "Wrap it in parentheses, e.g. (-a) instead of -a.",
    "*": "No URL, selector or @ref starts that way — check the value.",
}


def bad_operand(**values) -> str | None:
    """Reject a caller-supplied argv element the CLI would take as one of ITS options.

    The CLI scans the whole argv for options — verified on 0.27.1: ``fill '#q' '--help'``
    prints help instead of filling, and ``--headed`` / ``--allow-file-access`` /
    ``--profile=…`` in a value would change how a first launch happens, so model-, page- or
    toolbar-chosen text could set a launch flag (CWE-88). But it only swallows what LOOKS
    like an option: ``-5``, ``-$50.00``, ``- buy milk``, a lone ``-`` or ``--`` all go through
    untouched, and ``eval '-1'`` returns -1. So the rule is the option grammar — a dash, then
    a letter (``OPTION_LIKE``) — not "starts with a dash".

    Deliberately the grammar, not the CLI's current option list: an option upstream adds
    tomorrow is refused today. The price is that flag-shaped text the CLI happens not to know
    (``--foo``, ``-x=1``) is refused too, and the message says how to enter it anyway. The CLI
    has no ``--`` end-of-options escape (``open -- --help`` still prints help), so refusing is
    the only lever. ONE copy, used by the tools and by the panel's ``/nav`` route: the panel
    once had none. Returns an error string, or None.
    """
    for what, value in values.items():
        text = str(value)
        if OPTION_LIKE.match(text):
            return (f"Error: {what} {text[:80]!r} looks like a command-line option (a dash and "
                    f"then a letter), and the agent-browser CLI reads options anywhere in a "
                    f"command, so it would be taken as a flag rather than as your {what}. "
                    f"{WORKAROUND.get(what, WORKAROUND['*'])}")
    return None
