"""A uv-made venv has no pip; install deps goes through ``uv pip`` instead (#4263).

PROTO.md's setup is ``uv sync``, whose venvs ship without pip, so ``python -m pip install``
failed there, and every optional dep was reported failed while the route still said ok. The
uv stand-in here is a real executable on PATH that records the argv it was run with, and the
install is the real ``install_deps``; only pip's absence is simulated.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import runtime.state as rs
from graph.plugins import installer

PID = "uvdemo"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the uv stand-in is a POSIX script")


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    live = tmp_path / "installed"
    (live / PID).mkdir(parents=True)
    (live / PID / "protoagent.plugin.yaml").write_text(
        f"id: {PID}\nname: Uv Demo\nversion: 0.1.0\nrequires_pip:\n  - protoagent-fake-dep-{PID}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(live))
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    return tmp_path


def _no_pip(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a, **k: None if name == "pip" else real(name, *a, **k)
    )


def _fake_uv(tmp_path: Path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv.argv"
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\n', encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


def test_no_pip_installs_through_uv_into_this_interpreter(plugin, monkeypatch):
    _no_pip(monkeypatch)
    log = _fake_uv(plugin, monkeypatch)
    newly: list[str] = []
    installer.install_deps(PID, newly_installed=newly)
    argv = log.read_text(encoding="utf-8").splitlines()
    assert argv == ["pip", "install", "--python", sys.executable, "--", f"protoagent-fake-dep-{PID}"]
    assert newly == [f"protoagent-fake-dep-{PID}"]


def test_pip_present_keeps_pip(monkeypatch, tmp_path):
    _fake_uv(tmp_path, monkeypatch)  # uv on PATH doesn't win over a venv that has pip
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a, **k: object() if name == "pip" else real(name, *a, **k)
    )
    assert installer._host_pip() == [sys.executable, "-m", "pip", "install"]


def test_no_pip_and_no_uv_names_the_fix(monkeypatch):
    _no_pip(monkeypatch)
    monkeypatch.setattr(installer.shutil, "which", lambda name, *a, **k: None)
    assert installer._host_pip() == [sys.executable, "-m", "pip", "install"]
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('/x/python: No module named pip'); sys.exit(1)"],
        capture_output=True,
        text=True,
    )
    msg = installer._pip_failure(proc, ["openpyxl", "reportlab"])
    assert "has no pip" in msg and f"uv pip install --python {sys.executable} openpyxl reportlab" in msg
