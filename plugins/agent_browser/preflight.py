"""Setup preflight — is the `agent-browser` CLI (and the Chrome it drives) actually usable?

This plugin is a shell over an external native binary that nothing in protoAgent
installs. Before #3451 a missing CLI was invisible until the agent tried to browse and
got ``Error: 'agent-browser' not on PATH`` back into its tool loop, while the console's
operator status said ``warnings: []`` — the exact failure mode the setup-gap seam was
built for (``graph/plugins/setup_gaps.py``). Now ``register()`` probes at load time and
reports through ``registry.report_setup_gap`` so the gap is an operator **banner**, and
the tools re-probe when one fails so the banner appears mid-session and self-clears the
moment the operator installs the binary — no restart.

Two gaps, one per concern, because they have different fixes:

* ``cli``    — the binary isn't resolvable. Fix: install it, or point ``binary`` at a
  path. That one carries a declarative ``plugin_config`` action so the console can offer
  the plugin's own config section (``action=`` SINGULAR — the host sanitizes one action
  or a list off that kwarg; a plugin that guesses ``actions=`` silently loses its button).
* ``chrome`` — the CLI is there but has no browser to drive. Fix: ``agent-browser
  install``. No action: the remedy is a CLI command, and ``ACTION_KINDS`` is a closed
  vocabulary of config/settings targets, not commands.

The Chrome answer comes from the CLI's own ``doctor`` (``--json --quick --offline``: no
live launch test, no network probes — ~40ms), read by check id rather than by scraping
the human table. Anything unexpected — an older CLI with no ``--json``, a non-zero exit,
unparseable output — degrades to ``unknown`` and reports NO Chrome gap: a false banner
about the operator's browser is worse than a missing one.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("protoagent.plugins.agent_browser")

CLI_GAP = "cli"
CHROME_GAP = "chrome"

INSTALL_HINT = "npm i -g agent-browser && agent-browser install"

# The doctor check that answers "is there a browser to drive". Keyed by id, not by
# parsing the pretty table, so a cosmetic output change can't turn into a false banner.
_CHROME_CHECK = "chrome.installed"

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

    @property
    def cli_ok(self) -> bool:
        return bool(self.cli_path)

    def as_dict(self) -> dict:
        return {"binary": self.binary, "cli_path": self.cli_path, "cli_version": self.cli_version,
                "cli_ok": self.cli_ok, "chrome": self.chrome, "chrome_detail": self.chrome_detail}


def resolve_binary(binary: str) -> str:
    """The absolute path the CLI would run from, or ``""``.

    ``shutil.which`` covers the PATH spelling (``agent-browser``). An operator who
    pinned an absolute/relative path in ``binary`` gets that checked directly — which
    is the case a bare ``which`` misses, and the one the desktop app needs, since the
    Tauri shell only sees an nvm-installed CLI through the login shell's PATH."""
    name = str(binary or "").strip()
    if not name:
        return ""
    found = shutil.which(name)
    if found:
        return str(Path(found))
    candidate = Path(name).expanduser()
    if os.sep in name or (os.altsep and os.altsep in name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return ""


def _cli_version(path: str, timeout: float) -> str:
    try:
        p = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip().splitlines()[0].strip() if p.stdout else ""


def _chrome_status(path: str, timeout: float) -> tuple[str, str]:
    """``(chrome, detail)`` from ``agent-browser doctor --json --quick --offline``.

    ``--quick`` skips doctor's live headless launch and ``--offline`` its CDN probe, so
    this is a filesystem/state read. Doctor also tidies stale daemon socket/pid sidecar
    files as a side effect (its documented no-``--fix`` behavior); that is cleanup of
    files whose process is already gone, not a repair, and nothing else here mutates."""
    try:
        p = subprocess.run([path, "doctor", "--json", "--quick", "--offline"],
                           capture_output=True, text=True, timeout=timeout)
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
    if a wedged ``--version`` spent it all, the Chrome probe is skipped (``unknown``)."""
    cfg = cfg or {}
    binary = str(cfg.get("binary") or "agent-browser")
    try:
        path = resolve_binary(binary)
    except Exception:  # noqa: BLE001 — a preflight must never break plugin loading
        log.exception("[agent_browser] resolving %r failed", binary)
        return Probe(binary=binary)
    if not path:
        return Probe(binary=binary)
    budget = max(0.1, float(timeout))
    deadline = time.monotonic() + budget
    try:
        version = _cli_version(path, budget / 2)
        remaining = deadline - time.monotonic()
        chrome, detail = _chrome_status(path, remaining) if remaining > 0 else ("unknown", "")
    except Exception:  # noqa: BLE001
        log.exception("[agent_browser] probing %r failed", path)
        return Probe(binary=binary, cli_path=path)
    return Probe(binary=binary, cli_path=path, cli_version=version, chrome=chrome, chrome_detail=detail)


def gaps(p: Probe) -> list[tuple[str, str | None, object]]:
    """``(key, message | None, action | None)`` for every gap key this plugin owns.

    Always returns BOTH keys so a fixed setup clears its banner: ``message=None`` is the
    clear signal, and reporting it unconditionally is what makes the banner self-heal
    when the operator installs the binary mid-session.
    """
    if not p.cli_ok:
        cli_msg = (f"the {p.binary!r} CLI isn't on PATH, so the browser tools and the Browser panel "
                   f"can't run. Install it with `{INSTALL_HINT}`, or set the plugin's `binary` "
                   f"setting to its full path.")
        action = {"kind": "plugin_config", "label": "Set the CLI path", "fields": ["binary"]}
        return [(CLI_GAP, cli_msg, action), (CHROME_GAP, None, None)]
    chrome_msg = None
    if p.chrome == "missing":
        detail = f" ({p.chrome_detail})" if p.chrome_detail else ""
        chrome_msg = (f"{p.binary} is installed but has no Chrome to drive{detail} — run "
                      f"`agent-browser install` to download Chrome for Testing.")
    return [(CLI_GAP, None, None), (CHROME_GAP, chrome_msg, None)]


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
