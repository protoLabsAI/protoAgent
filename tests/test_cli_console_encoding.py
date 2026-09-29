"""The CLI must not crash on a Windows cp1252 console (v0.184.0 desktop build).

The frozen ``protoagent-server fleet --help`` raised ``UnicodeEncodeError`` on the
Windows runner: a help string carried "▸" and a Windows pipe/console defaults to
cp1252. Two layers keep it from recurring: the entrypoints relax stdout/stderr to
``errors="replace"`` when their encoding can't carry our glyphs, and the argparse help
of the forwarded CLIs stays cp1252-encodable in the first place."""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server import cli as server_cli

ROOT = Path(__file__).resolve().parent.parent


def _cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_ensure_console_encoding_relaxes_a_cp1252_stream(monkeypatch):
    out, err = _cp1252_stream(), _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    with pytest.raises(UnicodeEncodeError):
        print("Settings ▸ Devices ✓", file=out)

    server_cli.ensure_console_encoding()

    print("Settings ▸ Devices ✓", file=out)
    print("⚠ warning", file=err)
    out.flush()
    assert out.buffer.getvalue().decode("cp1252").strip() == "Settings ? Devices ?"
    assert out.encoding == "cp1252"  # only the errors policy changes


def test_ensure_console_encoding_leaves_utf8_and_odd_streams_alone(monkeypatch):
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", utf8)
    monkeypatch.setattr(sys, "stderr", None)  # pythonw / a windowed frozen build
    server_cli.ensure_console_encoding()
    assert utf8.errors == "strict"

    monkeypatch.setattr(sys, "stdout", io.StringIO())  # no reconfigure()
    server_cli.ensure_console_encoding()


def test_fleet_help_survives_a_cp1252_stdout(monkeypatch):
    out = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    server_cli.ensure_console_encoding()
    with pytest.raises(SystemExit) as exc:
        server_cli.dispatch(["fleet", "--help"])
    assert exc.value.code == 0
    out.flush()
    assert "usage: protoagent fleet" in out.buffer.getvalue().decode("cp1252")


def _all_help(parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    """``format_help()`` of ``parser`` and every nested subparser."""
    helps = [(parser.prog, parser.format_help())]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in dict.fromkeys(action.choices.values()):  # aliases share a parser
                helps.extend(_all_help(sub))
    return helps


@pytest.mark.parametrize(
    "module,builder",
    [
        ("graph.fleet.cli", "_build_parser"),
        ("graph.plugins.cli", "_build_parser"),
        ("graph.workspaces.cli", "_build_parser"),
        ("graph.skills.cli", "_build_parser"),
        ("deck.pair", "_parser"),
    ],
)
def test_cli_help_is_cp1252_encodable(module, builder):
    import importlib

    parser = getattr(importlib.import_module(module), builder)()
    bad = []
    for prog, text in _all_help(parser):
        try:
            text.encode("cp1252")
        except UnicodeEncodeError as exc:
            bad.append(f"{prog}: {text[exc.start : exc.end]!r} in …{text[max(0, exc.start - 40) : exc.end + 10]!r}")
    assert not bad, "non-cp1252 characters in CLI help (a Windows console can't print them):\n" + "\n".join(bad)


@pytest.mark.platform_sensitive
def test_frozen_smoke_path_fleet_help_under_cp1252():
    """The exact desktop-build smoke leg: ``python -m server fleet --help`` (the frozen
    binary's entry) with a cp1252 pipe, as on the Windows runner."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [sys.executable, "-m", "server", "fleet", "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode("cp1252", "replace")
    assert b"usage: protoagent fleet" in proc.stdout
