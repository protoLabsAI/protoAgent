"""``protoagent fleet --self-check`` — prove the deck loads and paints, with no terminal (#3498).

The deck refuses to start without a TTY, so nothing in the desktop build ever ran it: a
PyInstaller miss — a Textual widget resolved through ``textual.widgets.__getattr__``, the
platform driver ``App.__init__`` imports by platform, a Rich cell table loaded by name at
render time, a deck module reached only through ``importlib`` — shipped green on every leg.

This builds the real :class:`~deck.app.FleetDeck` over an in-memory offline roster (no hub,
no disk), drives it under Textual's headless driver through the screens an operator opens
first — roster, filter prompt, member detail, hub tree — and returns 0 only when each one
composed and the roster painted a frame with its member in it. Any failure prints the
traceback (it names the missing module) and returns 1.

Imports Textual (through ``deck.app``), so ``graph.fleet.cli`` reaches it by name only, like
the deck itself: ``--help`` and the non-interactive verbs never load it.
"""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import textual
from textual.widgets import DataTable

from deck.app import DetailScreen, FilterScreen, FleetDeck, RosterScreen
from deck.data import OfflineBackend
from deck.discovery import HubRow
from deck.hubs import HubTreeScreen

MEMBER = {"name": "selfcheck", "id": "selfcheck-0000", "port": 7999, "pid": None, "running": False}
HUB = HubRow(name="selfcheck-hub", root=None, url="http://127.0.0.1:7999", port=7999, presence="stopped", source="flag")
SIZE = (120, 36)
STEP_S = 20.0  # per screen; a cold frozen binary on a CI runner is slow, not stuck
TOTAL_S = 90.0


class SelfCheckFailed(Exception):
    """A screen did not reach the state the check waits for."""


def _refuse(name: str) -> dict:
    return {"ok": False, "agent": {"name": name}, "reason": "self-check: nothing is started or stopped"}


def _backend() -> OfflineBackend:
    return OfflineBackend(
        status=lambda: [dict(MEMBER)],
        start=_refuse,
        stop=_refuse,
        fleet_json=Path("(self-check)"),
        reason="self-check",
    )


def _roster_names(app: FleetDeck) -> list[str]:
    try:
        table = app.screen.query_one("#roster", DataTable)
    except Exception:  # noqa: BLE001 — not composed yet, or another screen is on top
        return []
    return [str(table.get_row(key)[1]) for key in table.rows]


async def _until(pilot: Any, ok: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + STEP_S
    while not ok():
        if not pilot.app.is_running:
            # it died (run_test re-raises the error that killed it, which names the module)
            raise SelfCheckFailed(f"{what}: the deck exited")
        if time.monotonic() > deadline:
            raise SelfCheckFailed(what)
        await pilot.pause(0.05)


async def _drive(app: FleetDeck) -> list[str]:
    painted: list[str] = []
    async with app.run_test(size=SIZE) as pilot:
        await _until(pilot, lambda: _roster_names(app) == [MEMBER["name"]], "the roster never painted its member")
        frame = app.export_screenshot()  # a full render of the current frame, through Rich
        if MEMBER["name"] not in frame:
            raise SelfCheckFailed("the roster frame rendered without its member")
        painted.append("roster")

        await pilot.press("slash")
        await _until(pilot, lambda: isinstance(app.screen, FilterScreen), "the filter prompt never opened")
        await pilot.press("escape")
        await _until(pilot, lambda: isinstance(app.screen, RosterScreen), "the filter prompt never closed")
        painted.append("filter")

        # offline mode refuses the detail key; the screen itself still has to compose
        app.push_screen(DetailScreen(dict(MEMBER)))
        await _until(pilot, lambda: isinstance(app.screen, DetailScreen), "the member detail never opened")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await _until(pilot, lambda: isinstance(app.screen, RosterScreen), "the member detail never closed")
        painted.append("detail")

        app.hub_rows = [HUB]  # seeded, so opening the tree reads nothing from this box
        await pilot.press("H")
        await _until(pilot, lambda: isinstance(app.screen, HubTreeScreen), "the hub tree never opened")
        await _until(pilot, lambda: app.screen.query_one("#hubs", DataTable).row_count == 1, "the hub tree never painted its hub")
        await pilot.press("escape")
        await _until(pilot, lambda: isinstance(app.screen, RosterScreen), "the hub tree never closed")
        painted.append("hubs")

        await pilot.press("q")
    return painted


def run() -> int:
    """Open the deck headlessly, paint its first screens, exit. 0 = the deck works here."""
    try:
        app = FleetDeck(_backend(), poll_s=0, offline=True)
        driver = app.driver_class.__name__  # the platform driver App.__init__ imported
        painted = asyncio.run(asyncio.wait_for(_drive(app), TOTAL_S))
        if app.return_code:
            raise SelfCheckFailed(f"the deck exited with code {app.return_code}")
        version = textual.__version__  # read from the bundled dist-info, lazily
    except Exception as exc:  # noqa: BLE001 — every failure is the answer; the traceback names the module
        traceback.print_exc()
        print(f"fleet deck self-check FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"fleet deck self-check ok: textual {version}, {driver}, painted {', '.join(painted)}")
    return 0
