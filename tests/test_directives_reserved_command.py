"""`/directives` is a core chat command (server/chat_dispatch.py handles it before
plugin commands, ADR 0127 D6), so a plugin must not be able to register it: the real
registry would warn-and-skip (leaving a dead `/directives` out of ``chat_commands`` and
the palette) and the host-free testkit must raise so a drifting plugin's registration
fails its own CI loudly. `/goal` and `/lifecycle` stay reserved alongside it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from graph.plugins.registry import PluginRegistry
from graph.plugins.testkit import FakeRegistry


async def _h(rest, session_id):
    return "nope"


# --- real registry: warn + skip ---------------------------------------------


def test_registry_skips_directives_and_warns(caplog) -> None:
    reg = PluginRegistry("p", Path("/tmp"))

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        reg.register_chat_command("directives", _h)

    assert "directives" not in reg.chat_commands  # dead command never lands
    assert "/directives is reserved" in caplog.text


def test_registry_skips_directives_any_casing(caplog) -> None:
    reg = PluginRegistry("p", Path("/tmp"))

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        reg.register_chat_command("Directives", _h)  # slugified + lowercased first

    assert "directives" not in reg.chat_commands
    assert "/directives is reserved" in caplog.text


def test_registry_still_reserves_goal_and_lifecycle(caplog) -> None:
    reg = PluginRegistry("p", Path("/tmp"))

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        reg.register_chat_command("goal", _h)
        reg.register_chat_command("lifecycle", _h)

    assert "goal" not in reg.chat_commands
    assert "lifecycle" not in reg.chat_commands


def test_registry_allows_a_non_reserved_token() -> None:
    reg = PluginRegistry("p", Path("/tmp"))

    reg.register_chat_command("directive", _h)  # singular — NOT reserved
    assert "directive" in reg.chat_commands


# --- host-free testkit: raise -----------------------------------------------


def test_testkit_raises_for_directives() -> None:
    reg = FakeRegistry(plugin_id="p")

    try:
        reg.register_chat_command("directives", _h)
    except ValueError as exc:
        assert "directives" in str(exc)
    else:  # pragma: no cover - a silent accept would ship a dead command green
        raise AssertionError("testkit.register_chat_command must raise for the reserved /directives")

    assert "directives" not in reg.chat_commands


def test_testkit_raises_for_directives_any_casing() -> None:
    reg = FakeRegistry(plugin_id="p")

    try:
        reg.register_chat_command("Directives", _h)
    except ValueError as exc:
        assert "directives" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("testkit.register_chat_command must raise for the reserved /directives")


def test_testkit_still_raises_for_goal_and_lifecycle() -> None:
    reg = FakeRegistry(plugin_id="p")

    for token in ("goal", "lifecycle"):
        try:
            reg.register_chat_command(token, _h)
        except ValueError as exc:
            assert token in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"testkit.register_chat_command must raise for the reserved /{token}")


def test_testkit_allows_a_non_reserved_token() -> None:
    reg = FakeRegistry(plugin_id="p")

    reg.register_chat_command("directive", _h)  # singular — NOT reserved
    assert "directive" in reg.chat_commands
