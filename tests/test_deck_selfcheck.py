"""`protoagent fleet --self-check` and the desktop build's deck smoke (#3498).

The deck refuses to start without a terminal, so nothing ran it from the frozen desktop
binary: a PyInstaller miss shipped green on every leg. The self-check opens the real deck
under Textual's headless driver over an in-memory roster and paints its first screens; the
desktop build runs it (with the fleet verbs) through `scripts/fleet_deck_smoke.py --bin`.
These pin that it passes in the dev env and that it fails on the misses it exists to catch.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from graph.fleet import cli

REPO = Path(__file__).resolve().parents[1]


def test_self_check_opens_the_deck_and_paints_its_first_screens(capsys):
    assert cli.run_fleet_cli(["--self-check"]) == 0
    out = capsys.readouterr().out
    assert "fleet deck self-check ok" in out
    assert "painted roster, filter, detail, hubs" in out


def test_self_check_is_hidden_and_takes_no_verb(capsys):
    with pytest.raises(SystemExit) as help_exit:
        cli.run_fleet_cli(["--help"])
    assert help_exit.value.code == 0
    assert "--self-check" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as verb_exit:
        cli.run_fleet_cli(["--self-check", "ls"])
    assert verb_exit.value.code == 2


def test_self_check_in_a_build_without_the_deck_prints_the_same_hint_as_the_deck(monkeypatch, capsys):
    real = importlib.import_module

    def fake(name, *a, **kw):
        if name == "deck.selfcheck":
            raise ModuleNotFoundError("No module named 'textual'", name="textual")
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", fake)
    assert cli.run_fleet_cli(["--self-check"]) == 2
    assert "not available in this build (textual)" in capsys.readouterr().err


def test_a_screen_that_cannot_compose_fails_the_self_check(monkeypatch, capsys):
    """Opening the screens is the point: a deck whose hub tree dies on open imports fine."""
    from deck import hubs

    def broken(self):
        raise ModuleNotFoundError("No module named 'textual.widgets._tree'", name="textual.widgets._tree")

    monkeypatch.setattr(hubs.HubTreeScreen, "compose", broken)
    assert cli.run_fleet_cli(["--self-check"]) == 1
    err = capsys.readouterr().err
    assert "fleet deck self-check FAILED" in err and "textual.widgets._tree" in err


_BLOCK_RICH_TABLES = """
import importlib.abc, sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.startswith("rich._unicode_data.unicode"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)

sys.meta_path.insert(0, Block())
import deck.app  # an import-only check passes: Rich loads its cell table by name at RENDER time
import deck.selfcheck
from graph.fleet import cli

deck.selfcheck.STEP_S = 3.0  # the dead roster never paints; do not wait out the CI budget
print("rc", cli.run_fleet_cli(["--self-check"]))
"""


def test_a_module_loaded_only_at_render_time_fails_the_self_check(tmp_path):
    """Why the self-check renders instead of importing: Rich resolves its unicode cell table
    with `import_module` on the first wide/non-ASCII cell — a PyInstaller scan cannot see it,
    and `import deck.app` never touches it. Blocked, the import still succeeds and the
    self-check must fail."""
    env = dict(os.environ, PYTHONPATH=str(REPO), PROTOAGENT_HOME=str(tmp_path / "inst"), PROTOAGENT_BOX_ROOT=str(tmp_path / "box"))
    out = subprocess.run([sys.executable, "-c", _BLOCK_RICH_TABLES], env=env, capture_output=True, text=True, cwd=str(REPO), timeout=120)
    assert out.stdout.strip().splitlines()[-1:] == ["rc 1"], (out.stdout[-500:], out.stderr[-1500:])
    assert "rich._unicode_data.unicode" in out.stderr


def test_the_desktop_smoke_passes_against_the_dev_server():
    """`scripts/fleet_deck_smoke.py` without `--bin` runs `python -m server` — the same
    checks the desktop build runs against the frozen binary, so a check that could never
    pass fails here, not on a paid macOS/Windows leg."""
    out = subprocess.run([sys.executable, str(REPO / "scripts" / "fleet_deck_smoke.py"), "--timeout", "120"], capture_output=True, text=True, cwd=str(REPO), timeout=600)
    assert out.returncode == 0, (out.stdout[-3000:], out.stderr[-1500:])
    assert "fleet deck smoke: 4/4 passed" in out.stdout
