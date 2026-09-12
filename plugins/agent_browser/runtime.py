"""Shared runtime helpers — build the `agent-browser` global launch flags from config.

Used by BOTH the browser tools (`browser_open`) and the panel's nav route, so a session
launched from the console Start button gets the same headed / profile / device / anti-
detection setup as one the agent opens. Kept dependency-free so importing it never drags
in langchain (the panel imports it too).

Flags apply at **session launch** (the first `open`). A session already running without
them keeps its old setup until it's closed and reopened.
"""

from __future__ import annotations

# A realistic desktop Chrome UA (no "HeadlessChrome" giveaway). Used for stealth when
# running headless and no explicit user_agent is set; override with `user_agent`.
_STEALTH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


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
    if int(cfg.get("max_output", 0) or 0) > 0:
        f += ["--max-output", str(int(cfg["max_output"]))]

    # ── anti-detection ──────────────────────────────────────────────────────────
    # `stealth` layers on the common evasions: drop the `navigator.webdriver` automation
    # flag, and (when headless, where the UA says "HeadlessChrome") swap in a real desktop UA.
    ua = str(cfg.get("user_agent") or "").strip()
    if bool(cfg.get("stealth")):
        if "--disable-blink-features=AutomationControlled" not in args:
            args.append("--disable-blink-features=AutomationControlled")
        if not ua and not headed:
            ua = _STEALTH_UA
    if ua:
        f += ["--user-agent", ua]
    if args:
        f += ["--args", ",".join(args)]
    return f
