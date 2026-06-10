"""Autostart LaunchAgent generation — module-launch regression guard.

ADR 0023 promoted the single-file ``server.py`` into the ``server/``
package; ``autostart.py`` kept launching the deleted file, which made
the feature dead (the install-time exists() check always failed) and
left any legacy plist crash-looping at login. These tests pin the
plist to the ``python -m server`` module launch so a future rename
can't silently regress the launcher again, and exercise the
install-time existence check against the package layout.
"""
from __future__ import annotations

import plistlib

import autostart


def _render(**overrides) -> bytes:
    kwargs = dict(
        label="ai.protolabs.protoagent",
        python="/repo/.venv/bin/python",
        port=7870,
        working_dir="/repo",
        agent_name="protoagent",
        stdout_log="/repo/logs/autostart.out.log",
        stderr_log="/repo/logs/autostart.err.log",
    )
    kwargs.update(overrides)
    return autostart._render_launchagent_plist(**kwargs).encode("utf-8")


def test_plist_launches_the_server_module_not_server_py():
    data = plistlib.loads(_render())
    args = data["ProgramArguments"]

    assert args[0] == "/repo/.venv/bin/python"
    assert args[1:3] == ["-m", "server"], (
        "plist must launch `python -m server` (ADR 0023)"
    )
    assert not any(a.endswith("server.py") for a in args), (
        "stale single-file launch — server.py was deleted by ADR 0023"
    )
    # Port flag preserved, after the module args.
    assert args[3:] == ["--port", "7870"]


def test_plist_resolves_the_module_via_cwd_and_pythonpath():
    data = plistlib.loads(_render(working_dir="/some/repo"))

    # `python -m` puts the cwd on sys.path; PYTHONPATH is the same
    # belt-and-braces entrypoint.sh uses.
    assert data["WorkingDirectory"] == "/some/repo"
    assert data["EnvironmentVariables"]["PYTHONPATH"] == "/some/repo"


def test_plist_keepalive_semantics_preserved():
    data = plistlib.loads(_render())

    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}


def test_status_reports_the_server_package(monkeypatch):
    monkeypatch.setattr(autostart.platform, "system", lambda: "Darwin")
    status = autostart.autostart_status("protoagent")

    assert status["supported"] is True
    assert status["server_package"] == str(autostart.REPO_ROOT / "server")
    assert "server_path" not in status  # the old single-file key


def test_install_checks_for_the_server_package(monkeypatch, tmp_path):
    """With REPO_ROOT pointing somewhere without server/__init__.py,
    install must fail with a message naming the package — proving the
    exists() gate checks the post-ADR-0023 layout, not server.py."""
    monkeypatch.setattr(autostart.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(autostart, "REPO_ROOT", tmp_path)

    ok, message = autostart.install_autostart("protoagent", port=7870)

    assert ok is False
    assert str(tmp_path / "server" / "__init__.py") in message
