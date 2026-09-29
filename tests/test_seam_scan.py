"""The shared seam-guard scanner (tests/_seam_scan.py, #3856) catches every target spelling
and scans plugin test suites — planted stale patches, one per form."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._seam_scan import stale_patches, suite_files

_FORMS = {
    "dotted_string": 'monkeypatch.setattr("server.agent_init._moved", 1)\n',
    "alias": 'import server.agent_init as ai\nmonkeypatch.setattr(ai, "_moved", 1)\n',
    "from_import": 'from server import agent_init\nmonkeypatch.setattr(agent_init, "_moved", 1)\n',
    "attribute_path": 'import server.agent_init\nmonkeypatch.setattr(server.agent_init, "_moved", 1)\n',
    "mock_patch_object": 'import server.agent_init\nmock.patch.object(server.agent_init, "_moved", 1)\n',
    "import_module": 'import importlib\nmonkeypatch.setattr(importlib.import_module("server.agent_init"), "_moved", 1)\n',
    "import_module_alias": 'import importlib\nm = importlib.import_module("server.agent_init")\nsetattr(m, "_moved", 1)\n',
    "helper": (
        "import importlib\n"
        "def _mod():\n    return importlib.import_module('server.agent_init')\n"
        'monkeypatch.setattr(_mod(), "_moved", 1)\n'
    ),
    "sys_modules": 'import sys\nmonkeypatch.setattr(sys.modules["server.agent_init"], "_moved", 1)\n',
    "sys_modules_alias": 'import sys\nai = sys.modules["server.agent_init"]\nmonkeypatch.delattr(ai, "_moved")\n',
}


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_every_target_spelling_is_caught(tmp_path: Path, form: str):
    planted = tmp_path / "test_planted.py"
    planted.write_text(_FORMS[form], encoding="utf-8")
    hits = stale_patches("server.agent_init", {"_moved"}, files=[planted])
    assert [h.name for h in hits] == ["server.agent_init._moved"], form


def test_unrelated_names_and_modules_are_not_flagged(tmp_path: Path):
    planted = tmp_path / "test_planted.py"
    planted.write_text(
        "import server.agent_init as ai\nimport server.settings_apply as sa\n"
        'monkeypatch.setattr(ai, "_live", 1)\nmonkeypatch.setattr(sa, "_moved", 1)\n'
        'monkeypatch.setattr("server.agent_init_x._moved", 1)\n',
        encoding="utf-8",
    )
    assert stale_patches("server.agent_init", {"_moved"}, files=[planted]) == []


def test_package_level_target(tmp_path: Path):
    """``server.<name>`` — the package re-export — is its own target."""
    planted = tmp_path / "test_planted.py"
    planted.write_text(
        'import server\nmonkeypatch.setattr(server, "_moved", 1)\nmock.patch("server._moved", 1)\n', encoding="utf-8"
    )
    assert [h.name for h in stale_patches("server", {"_moved"}, files=[planted])] == ["server._moved"] * 2


def test_from_server_import_chat_is_the_function_not_the_module(tmp_path: Path):
    """``server`` re-exports the ``chat`` FUNCTION under its submodule's name, so
    ``from server import chat`` is not an alias of the module."""
    planted = tmp_path / "test_planted.py"
    planted.write_text('from server import chat\nmonkeypatch.setattr(chat, "_moved", 1)\n', encoding="utf-8")
    assert stale_patches("server.chat", {"_moved"}, files=[planted]) == []


def test_state_touch_through_the_shim(tmp_path: Path):
    planted = tmp_path / "test_planted.py"
    planted.write_text("import server.chat\nserver.chat._REG.clear()\n", encoding="utf-8")
    assert [h.name for h in stale_patches("server.chat", set(), state={"_REG"}, files=[planted])] == [
        "server.chat._REG"
    ]


def test_suite_files_include_plugin_test_dirs(tmp_path: Path):
    for rel in (
        "tests/test_core.py",
        "tests/sub/test_nested.py",
        "plugins/demo/tests/test_plugin.py",
        "plugins/demo/tests_integration/test_deep.py",
        "plugins/demo/test_not_in_a_tests_dir.py",
        "plugins/demo/node_modules/tests/test_vendored.py",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    found = {p.relative_to(tmp_path).as_posix() for p in suite_files(tmp_path)}
    assert found == {
        "tests/test_core.py",
        "tests/sub/test_nested.py",
        "plugins/demo/tests/test_plugin.py",
        "plugins/demo/tests_integration/test_deep.py",
    }
