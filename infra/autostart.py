"""OS-level autostart for the protoAgent server.

Hooks the server into the OS so it launches on user login. Today
macOS is the only supported path (LaunchAgent plist); Linux and
Windows stubs return a clear "not yet supported" error so the
wizard surfaces that instead of silently failing.

Design notes:

- The source of truth for "should autostart be on?" is
  ``runtime.autostart_on_boot`` in ``langgraph-config.yaml``. This
  module only installs / removes the OS artifact — it doesn't
  decide policy. The wizard and drawer toggle the YAML value and
  call these functions to bring the OS state in sync.

- ``sys.executable`` is captured at install time so reinstalling
  after a venv rebuild picks up the new interpreter path. If a user
  recreates their venv without reinstalling, the LaunchAgent keeps
  pointing at the stale path and will fail at next login — noisy
  log but not catastrophic. Documented in the docs.

- Install is idempotent: ``install_autostart`` overwrites any
  prior plist so the same file always reflects current state, no
  stale LaunchAgents piling up.
"""

from __future__ import annotations

import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

REPO_ROOT = Path(__file__).parent.resolve()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def autostart_supported() -> tuple[bool, str]:
    """Is this platform a supported autostart target?

    Returns ``(True, "")`` on supported platforms, ``(False, reason)``
    otherwise. Wizard / drawer check this before offering the toggle.
    """
    system = platform.system()
    if system == "Darwin":
        return True, ""
    if system == "Linux":
        return False, "Linux autostart (systemd user unit) not yet implemented"
    if system == "Windows":
        return False, "Windows autostart (Task Scheduler) not yet implemented"
    return False, f"autostart not implemented for platform {system!r}"


def install_autostart(agent_name: str = "protoagent", port: int = 7870) -> tuple[bool, str]:
    """Install the OS artifact that runs the server on user login.

    Returns ``(ok, message)``. On success, ``message`` is a short
    human-readable note the UI can display; on failure it's the
    actual error (permission denied, launchctl exit code, etc).
    """
    ok, reason = autostart_supported()
    if not ok:
        return False, reason

    if platform.system() == "Darwin":
        return _install_macos_launchagent(agent_name, port)
    return False, "unreachable"  # autostart_supported already rejected


def uninstall_autostart(agent_name: str = "protoagent") -> tuple[bool, str]:
    """Remove the OS autostart artifact. Safe to call when nothing
    is installed — returns success in that case.
    """
    ok, reason = autostart_supported()
    if not ok:
        return False, reason

    if platform.system() == "Darwin":
        return _uninstall_macos_launchagent(agent_name)
    return False, "unreachable"


def autostart_status(agent_name: str = "protoagent") -> dict:
    """Report current on-disk state for diagnostics.

    The UI uses this to render accurate "autostart is currently
    on/off" without having to remember what it last wrote.
    """
    ok, reason = autostart_supported()
    if not ok:
        return {"supported": False, "installed": False, "reason": reason}

    if platform.system() == "Darwin":
        plist = _macos_plist_path(agent_name)
        return {
            "supported": True,
            "installed": plist.exists(),
            "plist_path": str(plist),
            "python": sys.executable,
            "server_package": str(REPO_ROOT / "server"),
        }
    return {"supported": False, "installed": False, "reason": "unreachable"}


# ---------------------------------------------------------------------------
# macOS — LaunchAgent plist
# ---------------------------------------------------------------------------


_SAFE_LABEL_RE = re.compile(r"[^a-z0-9_.-]+")


def _macos_label(agent_name: str) -> str:
    """Plist label — namespaced so it doesn't collide with system labels.

    Sanitizes the input: only ``[a-z0-9_.-]`` survive. Leading / trailing
    dots and hyphens are stripped so the resulting filename can't be
    a hidden file or look like a path-segment. Path-traversal
    characters like ``/`` and ``..`` are filtered here rather than at
    the filesystem layer so ``install_autostart(agent_name="../../x")``
    can't escape ``~/Library/LaunchAgents/``.
    """
    sanitized = _SAFE_LABEL_RE.sub("-", agent_name.lower()).strip("-.")
    if not sanitized:
        sanitized = "protoagent"
    return f"ai.protolabs.{sanitized}"


def _macos_plist_path(agent_name: str) -> Path:
    home = Path.home()
    return home / "Library" / "LaunchAgents" / f"{_macos_label(agent_name)}.plist"


def _install_macos_launchagent(agent_name: str, port: int) -> tuple[bool, str]:
    """Write the plist and ``launchctl load`` it.

    Unload-then-load (rather than a bootstrap-replace dance) is the
    simplest idempotent recipe that works across macOS versions. A
    missing label on unload is a no-op.
    """
    python = sys.executable
    server_pkg = REPO_ROOT / "server" / "__init__.py"
    if not server_pkg.exists():
        return False, f"server package not found at {server_pkg}"

    label = _macos_label(agent_name)
    plist_path = _macos_plist_path(agent_name)
    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    plist = _render_launchagent_plist(
        label=label,
        python=python,
        port=port,
        working_dir=str(REPO_ROOT),
        agent_name=agent_name,
        stdout_log=str(log_dir / "autostart.out.log"),
        stderr_log=str(log_dir / "autostart.err.log"),
    )

    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Unload any prior incarnation first — silently ok if absent.
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, check=False,
    )

    plist_path.write_text(plist, encoding="utf-8")

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        err = (result.stderr.decode("utf-8", errors="replace")
               or result.stdout.decode("utf-8", errors="replace")
               or f"launchctl load exit={result.returncode}")
        return False, f"plist written but launchctl load failed: {err.strip()}"

    return True, f"installed • {plist_path.name} • runs `{shlex.quote(python)} -m server` on login"


def _uninstall_macos_launchagent(agent_name: str) -> tuple[bool, str]:
    plist_path = _macos_plist_path(agent_name)
    if not plist_path.exists():
        return True, "autostart was not installed"

    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, check=False,
    )

    try:
        plist_path.unlink()
    except OSError as e:
        return False, f"failed to remove plist: {e}"

    return True, f"uninstalled • removed {plist_path.name}"


def _render_launchagent_plist(
    *,
    label: str,
    python: str,
    port: int,
    working_dir: str,
    agent_name: str,
    stdout_log: str,
    stderr_log: str,
) -> str:
    """Render the plist XML.

    Every interpolated string is XML-escaped because several fields
    (``agent_name`` most notably) come from user input — a wizard
    user who names their agent ``bad<name>`` or ``me & co`` would
    otherwise produce a malformed or injection-vulnerable plist.
    ``port`` is an int so it's safe as-is, but we coerce+escape it
    anyway for consistency.

    ADR 0023: the server is launched as a module (``python -m server``),
    not the deleted single-file ``server.py``. ``WorkingDirectory`` is the
    repo root (``python -m`` puts the cwd on ``sys.path``) and
    ``PYTHONPATH`` is set to it as well — same belt-and-braces as
    ``entrypoint.sh`` — so the ``server`` package and its sibling
    top-level modules (``paths``, ``events``, ``graph``, …) resolve.
    """
    e = xml_escape
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{e(label)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{e(python)}</string>
        <string>-m</string>
        <string>server</string>
        <string>--port</string>
        <string>{e(str(port))}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{e(working_dir)}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AGENT_NAME</key>
        <string>{e(agent_name)}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>PYTHONPATH</key>
        <string>{e(working_dir)}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{e(stdout_log)}</string>
    <key>StandardErrorPath</key>
    <string>{e(stderr_log)}</string>
</dict>
</plist>
"""
