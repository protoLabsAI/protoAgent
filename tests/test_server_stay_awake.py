"""Exercise the serving-hold blocks in the nested server lifecycle hooks.

Like the fleet/sweep lifecycle tests, locate hooks by AST: constructing the real
app runs the entire CLI boot. Execute each complete enclosing statement so the
config guard and exception handling run without unrelated background services.
"""

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from infra import keep_awake

pytestmark = pytest.mark.platform_sensitive


def _run_hold_block(hook, call, config):
    source = Path(__file__).resolve().parents[1] / "src/server/__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == hook)
    statements = [
        statement for statement in handler.body
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == call for n in ast.walk(statement))
    ]
    assert len(statements) == 1
    block = ast.Module(body=statements, type_ignores=[])
    exec(compile(block, str(source), "exec"), {
        "STATE": SimpleNamespace(graph_config=config),
        "log": logging.getLogger("test.server.stay_awake"),
    })


@pytest.mark.parametrize("enabled", [None, False, True])
@pytest.mark.parametrize("held", [False, True])
def test_startup_only_acquires_when_opted_in(monkeypatch, caplog, enabled, held):
    start = Mock(return_value=held)
    monkeypatch.setattr(keep_awake, "start_serving_hold", start)
    config = None if enabled is None else SimpleNamespace(stay_awake=enabled)
    with caplog.at_level(logging.INFO):
        _run_hold_block("_scheduler_startup", "start_serving_hold", config)
    assert start.call_count == int(enabled is True)
    if enabled:
        assert f"serving hold: {'held' if held else 'not held'}" in caplog.text


@pytest.mark.parametrize("config", [None, SimpleNamespace(stay_awake=False), SimpleNamespace(stay_awake=True)])
def test_shutdown_always_releases(monkeypatch, config):
    stop = Mock()
    monkeypatch.setattr(keep_awake, "stop_serving_hold", stop)
    _run_hold_block("_scheduler_shutdown", "stop_serving_hold", config)
    stop.assert_called_once_with()


@pytest.mark.parametrize("phase,call", [
    ("startup", "start_serving_hold"),
    ("shutdown", "stop_serving_hold"),
])
def test_hold_errors_are_logged_and_do_not_escape(monkeypatch, caplog, phase, call):
    failing = Mock(side_effect=RuntimeError("hold failed"))
    monkeypatch.setattr(keep_awake, call, failing)
    _run_hold_block(f"_scheduler_{phase}", call, SimpleNamespace(stay_awake=True))
    failing.assert_called_once_with()
    assert f"serving hold {phase} failed" in caplog.text
    assert "hold failed" in caplog.text
