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
    # Keep the page rendering even when it isn't the focused, on-screen window. Chrome
    # throttles/pauses backgrounded, occluded or unfocused pages: their timers and
    # requestAnimationFrame stall and `document.visibilityState` reads "hidden", so rAF
    # animations never start (#4510). That hits a headed window the operator has clicked away
    # from AND every headless session — headless is the DEFAULT and is "unfocused" by
    # definition, so it needs these just as much. Merge them in BOTH modes, de-duped, after
    # any user args so the argv order stays stable. `--headed` itself stays headed-only.
    for a in ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
              "--disable-background-timer-throttling"):
        if a not in args:
            args.append(a)
    if headed:
        f.append("--headed")
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


# ── which browser this instance drives (per-instance isolation) ──────────────────
# The CLI keys everything — its daemon, its Chrome, its cookie jar — off a SESSION name
# (`AGENT_BROWSER_SESSION`, or `--session`), and with none set it uses "default". Every
# protoAgent instance on a machine is its own server process (each fleet member, the dev
# sandbox, the desktop hub), so leaving the name to the CLI put every agent on ONE Chrome:
# one agent saw another's page mid-task, parallel fills crossed between agents, and every
# agent shared every other agent's signed-in cookies. So the plugin names the session
# itself, from the instance's identity, and hands that name to EVERY subprocess it runs —
# the tools, the panel's /nav route and the screencast's `get cdp-url` — through
# ``cli_env``. One helper, so the three can't disagree about which browser is "ours".
#
# The name is `pa-<slug>-<h6>`: the slug is the readable instance id (`jobcoach-2e97`,
# `dev`, or `main` for the unscoped default/desktop hub) and h6 a sha1 prefix of the
# instance ROOT path. The root, not the id: two unscoped instances (a source checkout and
# the desktop hub) both have id "" but different roots. The root, not ``instance_uid()``:
# the uid is a file written on first read and is "" on an unwritable root, while the path
# is known without IO and is just as stable. Same root → same data → same browser, by
# design. An operator-set AGENT_BROWSER_SESSION still wins (that IS the operator naming it).

_SLUG_MAX = 20


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:_SLUG_MAX].strip("-")


def instance_session_name() -> str:
    """The agent-browser session name for THIS protoAgent instance — stable across restarts,
    distinct per instance root, and made only of ``[a-z0-9-]`` (the CLI allows letters,
    digits, ``-`` and ``_``). Short: the CLI's daemon socket is ``<dir>/<name>.sock``, and a
    unix socket path is capped near 104 bytes."""
    import hashlib

    try:
        from infra.paths import instance_id, instance_paths

        iid = instance_id()
        root = str(instance_paths().instance_root)
    except Exception:  # noqa: BLE001 — outside a host (a bare import): still isolate by cwd
        import os

        iid, root = "", os.getcwd()
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:6]
    return f"pa-{_slug(iid) or 'main'}-{digest}"


def shared_session_name() -> str:
    """The instance's MAIN session — what ``session_scope: shared`` uses, what the console
    Browser panel and screencast follow, and per_session's fallback for a call with no chat
    id: the operator's ``AGENT_BROWSER_SESSION`` when set, else ``instance_session_name()``."""
    import os

    return os.environ.get("AGENT_BROWSER_SESSION", "").strip() or instance_session_name()


def chat_session_name(session_id: str) -> str:
    """The per-chat session name under ``session_scope: per_session``: the instance name plus
    a stable sha1 prefix of the chat id, so two chats get two browsers AND two instances'
    chats can never land on one (a bare ``pa-<sha>`` of the chat id could collide across
    instances that reuse an id)."""
    import hashlib

    return f"{instance_session_name()}-{hashlib.sha1(str(session_id).encode('utf-8')).hexdigest()[:10]}"


def idle_timeout_ms(cfg: dict | None, *, chat: bool) -> int:
    """The CLI daemon's idle reaper for a session, in ms (0 = never reap).

    ``idle_timeout_min`` (default 30) governs the per-chat browsers ``per_session`` spawns —
    one per chat that ever browsed, which would otherwise live until the machine reboots.
    ``main_idle_timeout_min`` (default 0 = off) governs the instance's main session: the
    operator's signed-in tabs live there, and the panel's screencast talks to Chrome over CDP
    directly — it never sends the daemon a command, so watching the panel would NOT keep a
    reaped-on-idle main session alive."""
    key, default = ("idle_timeout_min", 30) if chat else ("main_idle_timeout_min", 0)
    minutes = number(cfg, key, default)
    return int(minutes * 60_000) if minutes > 0 else 0


def cli_env(session: str, idle_ms: int = 0) -> dict[str, str]:
    """The environment for one `agent-browser` subprocess: a COPY of this process's env with
    the session pinned (never a mutation of ``os.environ``, which is process-global and would
    race between concurrent chats), plus ``AGENT_BROWSER_IDLE_TIMEOUT_MS`` when a reaper is
    configured. Verified in the 0.27.1 source: the daemon reads that var ONCE, when it is
    spawned, and exits — closing its Chrome — after that many ms with no command; every
    command resets the timer. So it applies to the next daemon this session starts, not to
    one already running. 0 leaves the var as the process env has it (unset = never)."""
    import os

    env = {**os.environ, "AGENT_BROWSER_SESSION": session}
    if idle_ms > 0:
        env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = str(int(idle_ms))
    return env


# ── a `profile` dir can back ONE browser ─────────────────────────────────────────
# Chrome holds a `SingletonLock` in a user-data-dir; a second Chrome on the same dir exits
# 21 ("Failed to create a ProcessSingleton for your profile directory. … Aborting now to
# avoid profile corruption"). Verified live on 0.27.1: with `profile: /some/dir`, the
# second session's `open` fails with that in a long stderr whose cause sits past the
# tools' 500-char error cut. That's every second chat under per_session, or a second
# instance pointed at the same dir. (A Chrome profile NAME like "Default" is copied to a
# temp dir per launch by the CLI, so it never collides.)
_PROFILE_LOCK_MARKERS = ("ProcessSingleton", "SingletonLock")


def profile_in_use_error(err: str, cfg: dict | None, session: str) -> str | None:
    """A readable error when a launch failed because the configured ``profile`` dir is
    already open in another browser, else None."""
    if not any(m in (err or "") for m in _PROFILE_LOCK_MARKERS):
        return None
    profile = str((cfg or {}).get("profile") or "").strip() or "the configured profile directory"
    per_session = str((cfg or {}).get("session_scope") or "").strip().lower() == "per_session"
    fix = ("session_scope is per_session, so every chat starts its own browser and only one of "
           "them can hold that profile at a time. Finish (browser_close) the chat that has it, "
           "or clear the plugin's `profile` setting, or switch session_scope back to shared."
           if per_session else
           "Another browser — another protoAgent instance or another agent-browser session — "
           "already has it open. Close that browser, or give this agent its own `profile` dir.")
    return (f"Error: the browser profile {profile!r} is already in use (Chrome allows one browser "
            f"per profile directory), so agent-browser session {session!r} could not launch. {fix}")
