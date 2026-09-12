"""Setup preflight — is the `agent-browser` CLI (and the Chrome it drives) actually usable?

This plugin is a shell over an external native binary. Before #3451 a missing CLI was
invisible until the agent tried to browse and got ``Error: 'agent-browser' not on PATH``
back into its tool loop, while the console's operator status said ``warnings: []`` — the
exact failure mode the setup-gap seam was built for (``graph/plugins/setup_gaps.py``). Now
``register()`` probes at load time and reports through ``registry.report_setup_gap`` so the
gap is an operator **banner**, and the tools re-probe when one fails so the banner appears
mid-session and self-clears the moment the setup is fixed — no restart.

Two gaps, one per concern, because they have different fixes — and each fix is a BUTTON on
its banner (a ``plugin_setup`` action naming a step ``setup_steps`` registered), not a
terminal command the operator has to go and run:

* ``cli``    — no CLI resolves. **Download agent-browser** fetches the pinned,
  sha256-verified release into the box cache (``cli_fetch``); **Set the CLI path** opens
  the plugin's config (``binary``). While the download runs the banner says so with no
  button; a failure says why and offers Retry. A host with no upstream build, or a
  ``binary`` setting naming something else, gets the config action only — a download there
  would never be used. (The first browser command also downloads it, when
  ``cli_autofetch`` is on — see ``cli_for_run``.)
* ``chrome`` — the CLI is there but has no browser to drive. **Install Chrome** runs the
  CLI's own ``agent-browser install`` (``chrome_install``) — only on that click.

Actions are passed as ``action=`` (SINGULAR — the host sanitizes one action or a list off
that kwarg; a plugin that guesses ``actions=`` silently loses its buttons).

The CLI resolves in this order (``locate``): ``agent-browser`` (or whatever ``binary``
names) on PATH > an operator-pinned path in ``binary`` > the fetched binary, for the stock
name only. An operator's own install always wins over the download.

The Chrome answer comes from the CLI's own ``doctor`` (``--json --quick --offline``: no
live launch test, no network probes — ~40ms), read by check id rather than by scraping the
human table. Anything unexpected — an older CLI with no ``--json``, a non-zero exit,
unparseable output — degrades to ``unknown`` and reports NO Chrome gap: a false banner
about the operator's browser is worse than a missing one. The same check's message names
the Chrome it found (``Google Chrome for Testing 149.0.7827.55 at …``); the probe hands that
version to ``runtime`` so stealth mode can claim the Chrome that is actually driving pages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import chrome_install, cli_fetch, runtime

log = logging.getLogger("protoagent.plugins.agent_browser")

DEFAULT_BINARY = "agent-browser"
CLI_GAP = "cli"
CHROME_GAP = "chrome"

INSTALL_HINT = "npm i -g agent-browser && agent-browser install"

# The setup steps behind the gaps' buttons — registered in __init__, implemented in setup_steps.
STEP_DOWNLOAD_CLI = "download-cli"
STEP_INSTALL_CHROME = "install-chrome"

# The doctor check that answers "is there a browser to drive". Keyed by id, not by
# parsing the pretty table, so a cosmetic output change can't turn into a false banner.
_CHROME_CHECK = "chrome.installed"
# A full Chrome version inside that check's message.
_CHROME_VERSION_RE = re.compile(r"\b\d{2,4}\.\d+\.\d+\.\d+\b")

# The preflight runs inside register(), i.e. during BOOT. Two serial probes at 15 s each
# could stall boot ~30 s on a wedged binary, so the budget is TOTAL — shared by both calls,
# never per call. The real CLI answers both in ~40 ms: 6 s is ~150x headroom, not a guess.
PREFLIGHT_BUDGET_S = 6.0


@dataclass(frozen=True)
class Probe:
    """What a preflight found. ``chrome`` is a tri-state: ``ok`` / ``missing`` /
    ``unknown`` — unknown means "the CLI couldn't tell us", never "broken"."""

    binary: str
    cli_path: str = ""          # resolved absolute path; "" = not resolvable
    cli_version: str = ""       # "" when the CLI wouldn't answer --version
    chrome: str = "unknown"     # ok | missing | unknown
    chrome_detail: str = ""
    # Found, but the OS wouldn't start it: the npm launcher (`#!/usr/bin/env node`) with no
    # node on the host's PATH, a non-executable file, the wrong architecture.
    cli_error: str = ""
    # Where cli_path came from: "path" (PATH), "configured" (a path in `binary`), "fetched"
    # (cli_fetch's verified download); "" when nothing resolved.
    source: str = ""
    chrome_version: str = ""    # e.g. "149.0.7827.55" — from doctor's chrome.installed message

    @property
    def cli_ok(self) -> bool:
        return bool(self.cli_path) and not self.cli_error

    def as_dict(self) -> dict:
        return {"binary": self.binary, "cli_path": self.cli_path, "cli_version": self.cli_version,
                "cli_ok": self.cli_ok, "cli_error": self.cli_error, "chrome": self.chrome,
                "chrome_detail": self.chrome_detail, "source": self.source,
                "chrome_version": self.chrome_version}


def is_default(binary: str | None) -> bool:
    """True when ``binary`` is the stock command name (or blank) — the only case the fetched
    CLI stands in for. A path, or another name, is the operator's explicit choice."""
    return str(binary or "").strip() in ("", DEFAULT_BINARY)


def locate(binary: str) -> tuple[str, str]:
    """``(absolute path, source)`` for the CLI the plugin should run, or ``("", "")``.

    ``shutil.which`` covers the PATH spelling — and wins, so an operator's own install is
    always the one used. An operator who pinned an absolute/relative path in ``binary`` gets
    that checked directly (what a bare ``which`` misses, and what the desktop app needs when
    the Tauri shell can't see an nvm-installed CLI). Last, for the stock name only: the
    binary ``cli_fetch`` downloaded and verified."""
    name = str(binary or "").strip()
    if not name:
        return "", ""
    found = shutil.which(name)
    if found:
        return str(Path(found)), "path"
    if os.sep in name or (os.altsep and os.altsep in name):
        candidate = Path(name).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve()), "configured"
        return "", ""
    if name == DEFAULT_BINARY:
        try:
            fetched = cli_fetch.installed_path()
        except Exception:  # noqa: BLE001 — an unreadable cache is "not fetched", never a crash
            log.exception("[agent_browser] checking the fetched CLI failed")
            fetched = ""
        if fetched:
            return fetched, "fetched"
    return "", ""


def resolve_binary(binary: str) -> str:
    """The absolute path the CLI would run from, or ``""`` (see ``locate``)."""
    return locate(binary)[0]


def effective_binary(binary: str) -> str:
    """What to put in argv[0]. A CLI found on PATH is spawned BY NAME, exactly as before the
    download existed (the OS does the same lookup); a pinned path or the fetched CLI by its
    absolute path. Nothing resolvable → the configured name, so a spawn fails the ordinary
    way (``FileNotFoundError``) and the caller's missing-CLI path runs."""
    name = str(binary or DEFAULT_BINARY)
    path, source = locate(name)
    return path if path and source != "path" else name


def cli_for_run(binary: str, *, autofetch: bool, on_done=None) -> str:
    """``effective_binary`` — plus FIRST USE: when nothing resolves, ``binary`` is the stock
    name and ``autofetch`` is on, download the pinned CLI now (inline: every caller is
    already off the event loop) and resolve again. A download already in flight (the
    banner's button) is joined, not repeated; a FAILED one isn't retried on every call — the
    banner's Retry button is the operator's lever."""
    if resolve_binary(binary) or not autofetch or not is_default(binary):
        return effective_binary(binary)
    cli_fetch.ensure_cli(background=False, wait=cli_fetch.FETCH_TIMEOUT_S, on_done=on_done)
    return effective_binary(binary)


def chrome_version_of(detail: str) -> str:
    """``"149.0.7827.55"`` out of doctor's ``chrome.installed`` message, or ``""``."""
    m = _CHROME_VERSION_RE.search(detail or "")
    return m.group(0) if m else ""


def _cli_version(path: str, timeout: float) -> tuple[str, str]:
    """``(version, launch_error)``. An OSError here means the binary exists but can't be
    STARTED — which used to be swallowed as "no version", leaving a CLI that could never run
    reported as healthy: no banner, and every tool call hit the unrate-limited re-probe path."""
    try:
        p = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except OSError as e:
        return "", (e.strerror or str(e) or e.__class__.__name__)
    except subprocess.SubprocessError:
        return "", ""
    if p.returncode != 0:
        return "", ""
    # Whitespace-only stdout made `[0]` raise IndexError; probe() swallowed it and skipped
    # the Chrome check entirely (#3451 review).
    lines = (p.stdout or "").strip().splitlines()
    return (lines[0].strip() if lines else ""), ""


def _chrome_status(path: str, timeout: float) -> tuple[str, str]:
    """``(chrome, detail)`` from ``agent-browser doctor --json --quick --offline``.

    ``--quick`` skips doctor's live headless launch and ``--offline`` its CDN probe, so
    this is a filesystem/state read. Doctor also tidies stale daemon socket/pid sidecar
    files as a side effect (its documented no-``--fix`` behavior); that is cleanup of
    files whose process is already gone, not a repair, and nothing else here mutates."""
    try:
        p = subprocess.run([path, "doctor", "--json", "--quick", "--offline"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return "unknown", ""
    try:
        checks = json.loads(p.stdout or "{}").get("checks") or []
    except (ValueError, AttributeError):
        return "unknown", ""
    for check in checks:
        if isinstance(check, dict) and check.get("id") == _CHROME_CHECK:
            status = str(check.get("status") or "").lower()
            detail = str(check.get("message") or "").strip()
            if status == "pass":
                return "ok", detail
            if status in ("fail", "warn"):
                return "missing", detail
            return "unknown", detail
    return "unknown", ""


def probe(cfg: dict | None, *, timeout: float = PREFLIGHT_BUDGET_S) -> Probe:
    """Resolve the CLI and ask it about Chrome. Never raises, and never takes longer than
    ``timeout`` in TOTAL: ``--version`` gets at most half, ``doctor`` whatever is left, and
    if a wedged ``--version`` spent it all, the Chrome probe is skipped (``unknown``).
    Never downloads anything — resolving the fetched CLI only looks in the cache."""
    cfg = cfg or {}
    binary = str(cfg.get("binary") or DEFAULT_BINARY)
    try:
        path, source = locate(binary)
    except Exception:  # noqa: BLE001 — a preflight must never break plugin loading
        log.exception("[agent_browser] resolving %r failed", binary)
        return Probe(binary=binary)
    if not path:
        return Probe(binary=binary)
    budget = max(0.1, float(timeout))
    deadline = time.monotonic() + budget
    try:
        version, launch_error = _cli_version(path, budget / 2)
        if launch_error:
            return Probe(binary=binary, cli_path=path, cli_error=launch_error, source=source)
        remaining = deadline - time.monotonic()
        chrome, detail = _chrome_status(path, remaining) if remaining > 0 else ("unknown", "")
    except Exception:  # noqa: BLE001
        log.exception("[agent_browser] probing %r failed", path)
        return Probe(binary=binary, cli_path=path, source=source)
    chrome_version = chrome_version_of(detail) if chrome == "ok" else ""
    if chrome_version:
        runtime.note_chrome_version(chrome_version)
    return Probe(binary=binary, cli_path=path, cli_version=version, chrome=chrome, chrome_detail=detail,
                 source=source, chrome_version=chrome_version)


def _clip(text, limit: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _cli_gap(p: Probe):
    """``(message, action)`` for a CLI that doesn't resolve or won't start."""
    configure = {"kind": "plugin_config", "label": "Set the CLI path", "fields": ["binary"]}
    if p.cli_error:
        return (f"the {p.binary!r} CLI was found but can't be started ({p.cli_error}). If it's the "
                f"npm launcher script, its node interpreter isn't on the host's PATH — point the "
                f"plugin's `binary` setting at the native agent-browser binary, or put node on PATH."), configure
    if not is_default(p.binary):
        # The operator named something else; a download would never be the CLI we run.
        return (f"the {p.binary!r} CLI isn't on PATH, so the browser tools and the Browser panel "
                f"can't run. Install it with `{INSTALL_HINT}`, or set the plugin's `binary` "
                f"setting to its full path."), configure
    if cli_fetch.fetch_spec() is None:
        return (f"the {p.binary!r} CLI isn't on PATH, and {cli_fetch.unsupported_hint()} — "
                f"install it with `{INSTALL_HINT}`, or set the plugin's `binary` setting to its "
                f"full path."), configure
    version = cli_fetch.CLI_VERSION
    st = cli_fetch.fetch_state()
    if st.get("state") == "fetching":
        # No button while it runs: a second click can only join the same download.
        return (f"downloading the agent-browser CLI v{version} — the browser tools and the Browser "
                f"panel start working once it's verified and in place."), None
    download = {"kind": "plugin_setup", "step": STEP_DOWNLOAD_CLI, "label": "Download agent-browser"}
    if st.get("state") == "failed":
        return (f"downloading the agent-browser CLI v{version} failed ({_clip(st.get('error'), 120)}). "
                f"Retry, or install it yourself with `npm i -g agent-browser`."), [
                    {**download, "label": "Retry download"}, configure]
    return (f"the {p.binary!r} CLI isn't on PATH, so the browser tools and the Browser panel can't "
            f"run. Download the pinned, checksum-verified build (v{version}), install it yourself "
            f"with `npm i -g agent-browser`, or set the plugin's `binary` setting to its full "
            f"path."), [download, configure]


def _chrome_gap(p: Probe):
    """``(message | None, action)`` for a working CLI's Chrome."""
    if p.chrome != "missing":
        return None, None
    detail = f" ({_clip(p.chrome_detail, 80)})" if p.chrome_detail else ""
    st = chrome_install.state()
    if st.get("state") == "installing":
        return (f"installing Chrome for Testing for {p.binary} (a ~150 MB download) — this clears "
                f"when it's done."), None
    if not chrome_install.supported():
        return (f"{p.binary} is installed but has no Chrome to drive{detail}, and Chrome for "
                f"Testing has no Linux ARM64 build — install Chromium from your package manager."), None
    install = {"kind": "plugin_setup", "step": STEP_INSTALL_CHROME, "label": "Install Chrome"}
    if st.get("state") == "failed":
        return (f"installing Chrome for Testing failed ({_clip(st.get('error'), 140)}). Retry, or "
                f"run `agent-browser install` yourself."), {**install, "label": "Retry Chrome install"}
    return (f"{p.binary} is installed but has no Chrome to drive{detail} — install Chrome for "
            f"Testing (a ~150 MB download), or run `agent-browser install` yourself."), install


def gaps(p: Probe) -> list[tuple[str, str | None, object]]:
    """``(key, message | None, action | None)`` for every gap key this plugin owns.

    Always returns BOTH keys so a fixed setup clears its banner: ``message=None`` is the
    clear signal, and reporting it unconditionally is what makes the banner self-heal
    when the operator installs the binary mid-session. ``action`` is one action dict or a
    list of them.
    """
    if not p.cli_ok:
        message, action = _cli_gap(p)
        return [(CLI_GAP, message, action), (CHROME_GAP, None, None)]
    message, action = _chrome_gap(p)
    return [(CLI_GAP, None, None), (CHROME_GAP, message, action)]


def report(registry, cfg: dict | None, *, timeout: float = PREFLIGHT_BUDGET_S) -> Probe:
    """Probe and push the result through ``registry.report_setup_gap``. Returns the probe.

    Guarded with ``getattr`` per the seam's own contract, so the plugin still loads on a
    host that predates setup gaps."""
    p = probe(cfg, timeout=timeout)
    fn = getattr(registry, "report_setup_gap", None)
    if callable(fn):
        for key, message, action in gaps(p):
            try:
                fn(key, message, action=action) if action else fn(key, message)
            except Exception:  # noqa: BLE001 — a banner is never worth failing a load over
                log.exception("[agent_browser] reporting setup gap %r failed", key)
    return p


def hint(p: Probe) -> str:
    """The one-line operator-facing setup sentence, or ``""`` when nothing is missing.
    Shown in the Browser panel's empty state, so the console says the same thing as the
    banner instead of leaking a raw subprocess error."""
    for _key, message, _action in gaps(p):
        if message:
            return message
    return ""


def explain_note(note: str, cfg: dict | None) -> str:
    """Enrich a ``browser_stream`` note with the setup story when it's a setup problem.

    ``resolve_page_target`` is deliberately IO-only and reports ``"'ab' not on PATH"``;
    the panel should say what to do about it. Any other note (no session, no page open)
    is passed through untouched — those are normal states, not gaps."""
    text = str(note or "")
    if "not on PATH" not in text:
        return text
    return hint(probe(cfg)) or text
