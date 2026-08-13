"""``python -m server --config <path>`` was declared but never consumed by any
startup path — every boot silently used the default instance's config and
credentials regardless of what path was given (#2647). Pin the fix: passing
``--config`` now exits loudly instead of being silently accepted and ignored."""

from __future__ import annotations

import sys

import pytest

import server


def test_config_flag_exits_instead_of_silently_ignored(monkeypatch, capsys, tmp_path):
    bogus = tmp_path / "isolated-config.yaml"
    monkeypatch.setattr(sys, "argv", ["server", "--config", str(bogus)])

    with pytest.raises(SystemExit) as exc_info:
        server._main()

    assert exc_info.value.code == 2  # argparse's usage-error exit code
    stderr = capsys.readouterr().err
    assert "--config" in stderr
    assert "PROTOAGENT_HOME" in stderr  # points at the actual supported mechanism


def test_bare_invocation_never_reaches_the_config_error(monkeypatch):
    """Sanity check the guard is scoped to --config specifically — an
    unrelated bad flag must still fail as ordinary argparse usage error, not
    get swallowed by this check."""
    monkeypatch.setattr(sys, "argv", ["server", "--not-a-real-flag"])

    with pytest.raises(SystemExit) as exc_info:
        server._main()

    assert exc_info.value.code == 2
