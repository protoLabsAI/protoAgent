"""deck.manage + the roster's manage keys (#3471): create from an archetype, rename, delete
with the typed confirm and purge, a 409 as retryable, remotes (token masked, sent once),
roster order, and the offline refusals."""

from __future__ import annotations

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from deck import hub as deckhub
from deck.app import FleetDeck, RosterScreen
from deck.manage import DeleteModal, NewAgentModal, RemoteModal, RenameModal
from tests.test_deck_app import FakeBackend, _settle


async def _until(pilot, cond, timeout=4.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def _rows(app) -> list[str]:
    t = app.screen.query_one("#roster", DataTable)
    return [str(t.get_row_at(i)[1]) for i in range(t.row_count)]


@pytest.mark.asyncio
async def test_new_member_from_an_archetype_posts_the_consoles_body():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(app, pilot)
        await pilot.press("n")
        assert await _until(pilot, lambda: isinstance(app.screen, NewAgentModal))
        modal = app.screen
        assert modal.query_one("#archetype", Select).value == "basic" and [a["id"] for a in modal.archetypes] == ["basic", "pm"]
        await pilot.press("ctrl+s")  # no name yet: refused, stays open
        await pilot.pause(0.1)
        assert isinstance(app.screen, NewAgentModal) and "name is required" in str(modal.query_one("#hint", Static).content)
        modal.query_one("#name", Input).focus()
        await pilot.press(*"sc out", "ctrl+s")  # the hub's charset rule, checked before the round trip
        await pilot.pause(0.1)
        assert isinstance(app.screen, NewAgentModal) and "letters, digits" in str(modal.query_one("#hint", Static).content)
        modal.query_one("#name", Input).value = ""
        await pilot.press(*"scout")
        modal.query_one("#archetype", Select).value = "pm"
        await pilot.pause(0.1)
        assert "installs https://github.com/x/pm-archetype" in str(modal.query_one("#archetype-note", Static).content)
        modal.query_one("#start", Checkbox).value = False
        modal.query_one("#port", Input).value = "7911"
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        creates = [c for c in be.calls if c[0] == "create"]
        assert creates and creates[0][1] == {"name": "scout", "bundle": "https://github.com/x/pm-archetype", "inherit_config": True, "start": False, "soul": "You are a PM.", "requires_tools": ["github.write"], "port": 7911}
        assert isinstance(app.screen, RosterScreen) and "scout" in _rows(app)
        # the reserved name is refused before it reaches the hub
        await pilot.press("n")
        assert await _until(pilot, lambda: isinstance(app.screen, NewAgentModal))
        app.screen.query_one("#name", Input).focus()
        await pilot.press(*"host", "enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, NewAgentModal) and "reserved" in str(app.screen.query_one("#hint", Static).content)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(app.screen, RosterScreen) and len([c for c in be.calls if c[0] == "create"]) == 1


@pytest.mark.asyncio
async def test_rename_is_display_only_and_a_same_name_is_a_no_op():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        await pilot.press("j", "R")  # protoEngineer
        assert await _until(pilot, lambda: isinstance(app.screen, RenameModal))
        await pilot.press("enter")  # unchanged
        await pilot.pause(0.1)
        assert isinstance(app.screen, RosterScreen) and not any(c[0] == "rename" for c in be.calls)
        await pilot.press("R")
        assert await _until(pilot, lambda: isinstance(app.screen, RenameModal))
        inp = app.screen.query_one("#name", Input)
        inp.value = ""
        await pilot.press(*"Engineer Prime", "enter")  # a space: the hub would 400 — refused here, typing kept
        await pilot.pause(0.1)
        assert isinstance(app.screen, RenameModal) and "letters, digits" in str(app.screen.query_one("#hint", Static).content)
        assert app.screen.query_one("#name", Input).value == "Engineer Prime"
        app.screen.query_one("#name", Input).value = ""
        await pilot.press(*"engineer-prime", "enter")
        await _settle(app, pilot)
        assert ("rename", "protoEngineer-ba4c", "engineer-prime") in be.calls
        assert "engineer-prime" in _rows(app)
        # the hub's own name is not for the deck to change: the key is hidden and inert
        app.screen.query_one("#roster", DataTable).move_cursor(row=0)
        await pilot.pause(0.1)
        assert app.screen.check_action("rename", ()) is False and app.screen.check_action("delete", ()) is False
        await pilot.press("R")
        await pilot.pause(0.1)
        assert isinstance(app.screen, RosterScreen)


@pytest.mark.asyncio
async def test_delete_needs_the_typed_name_purge_is_separate_and_a_409_is_retryable():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    seen: list = []
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        app.notify = lambda msg, **kw: seen.append((msg, kw.get("severity", "information")))  # type: ignore[method-assign]
        await pilot.press("j", "j", "j", "d")  # Cindi (stopped)
        assert await _until(pilot, lambda: isinstance(app.screen, DeleteModal))
        modal = app.screen
        assert modal.query_one("#submit", Button).disabled
        await pilot.press(*"Cind", "enter")  # not the name yet
        await pilot.pause(0.1)
        assert isinstance(app.screen, DeleteModal) and "exactly" in str(modal.query_one("#hint", Static).content)
        await pilot.press("i")
        await pilot.pause(0.1)
        assert not modal.query_one("#submit", Button).disabled
        modal.query_one("#purge", Checkbox).value = True
        await pilot.press("enter")
        await _settle(app, pilot)
        assert ("remove", "Cindi-9f49", {"purge": True}) in be.calls
        assert "Cindi" not in _rows(app) and any("purged" in m for m, _ in seen)
        # a 409: stopped, workspace survived — retryable, not a failure
        be.remove_error = deckhub.HubRequestError("http://127.0.0.1:7870", 409, "workspace busy")
        app.screen.query_one("#roster", DataTable).move_cursor(row=2)  # old
        await pilot.press("d")
        assert await _until(pilot, lambda: isinstance(app.screen, DeleteModal))
        await pilot.press(*"old", "enter")
        await _settle(app, pilot)
        assert any("repeat the delete" in m and sev == "warning" for m, sev in seen)
        # still listed — and the re-poll shows what the hub did before the workspace refused: stopped
        table = app.screen.query_one("#roster", DataTable)
        assert "old" in _rows(app) and str(table.get_row_at(_rows(app).index("old"))[2]) == "stopped"


@pytest.mark.asyncio
async def test_remotes_add_edit_and_remove_with_the_token_sent_once():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        await pilot.press("a")
        assert await _until(pilot, lambda: isinstance(app.screen, RemoteModal))
        modal = app.screen
        assert modal.query_one("#token", Input).password  # masked
        await pilot.press(*"bo", "tab")
        modal.query_one("#url", Input).value = "https://bo.tail:7870"
        modal.query_one("#token", Input).value = "s3cret"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert ("remote_add", "bo", "https://bo.tail:7870", "s3cret") in be.calls
        assert "bo" in _rows(app)
        # edit: only the changed fields travel; blank token keeps the stored one
        rows = _rows(app)
        app.screen.query_one("#roster", DataTable).move_cursor(row=rows.index("bo"))
        await pilot.press("e")
        assert await _until(pilot, lambda: isinstance(app.screen, RemoteModal))
        assert app.screen.query_one("#token", Input).value == ""  # never shown again
        app.screen.query_one("#url", Input).value = "https://bo2.tail:7870"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert ("remote_update", "r-bo", {"url": "https://bo2.tail:7870"}) in be.calls
        # clear the token explicitly — but never together with a typed one
        app.screen.query_one("#roster", DataTable).move_cursor(row=rows.index("bo"))
        await pilot.press("e")
        assert await _until(pilot, lambda: isinstance(app.screen, RemoteModal))
        app.screen.query_one("#clear", Checkbox).value = True
        app.screen.query_one("#token", Input).value = "new"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, RemoteModal) and "not both" in str(app.screen.query_one("#hint", Static).content)
        app.screen.query_one("#token", Input).value = ""
        await pilot.press("enter")
        await _settle(app, pilot)
        assert ("remote_update", "r-bo", {"token": ""}) in be.calls
        # edit on a LOCAL member: the key is hidden and inert; remove a remote = unregister, typed confirm
        app.screen.query_one("#roster", DataTable).move_cursor(row=1)
        await pilot.pause(0.1)
        assert app.screen.check_action("edit_remote", ()) is False
        await pilot.press("e")
        await pilot.pause(0.1)
        assert isinstance(app.screen, RosterScreen)
        app.screen.query_one("#roster", DataTable).move_cursor(row=rows.index("bo"))
        await pilot.press("d")
        assert await _until(pilot, lambda: isinstance(app.screen, DeleteModal))
        assert not app.screen.query("#purge") and "unregisters" in app.screen.query(".manage-sub").first().render().plain
        await pilot.press(*"bo", "enter")
        await _settle(app, pilot)
        assert ("remote_remove", "r-bo") in be.calls and "bo" not in _rows(app)


@pytest.mark.asyncio
async def test_roster_order_moves_persist_a_complete_permutation():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        before = [a["id"] for a in be.roster]
        await pilot.press("j", "J")  # protoEngineer one down
        await _settle(app, pilot)
        moved = [c for c in be.calls if c[0] == "set_order"]
        assert moved and moved[0][1] == [before[0], before[2], before[1], before[3], before[4]]
        assert _rows(app)[1:3] == ["old", "protoEngineer"]
        app.screen.query_one("#roster", DataTable).move_cursor(row=0)
        await pilot.press("K")  # the host cannot move above the top: nothing sent
        await pilot.pause(0.2)
        assert len([c for c in be.calls if c[0] == "set_order"]) == 1
        # two quick presses move TWICE (the second computes from the optimistic order)
        app.screen.query_one("#roster", DataTable).move_cursor(row=2)  # protoEngineer, now third
        await pilot.press("J", "J")
        await _settle(app, pilot)
        orders = [c[1] for c in be.calls if c[0] == "set_order"]
        assert orders[-2].index("protoEngineer-ba4c") == 3 and orders[-1].index("protoEngineer-ba4c") == 4
        assert _rows(app)[-1] == "protoEngineer"
        # under a filter the keys are hidden and inert: a move would swap with a hidden neighbour
        app.screen._set_filter("proto")
        await pilot.pause(0.2)
        assert app.screen.check_action("move_down", ()) is False and app.screen.check_action("move_up", ()) is False
        n = len(orders)
        await pilot.press("J")
        await pilot.pause(0.2)
        assert len([c for c in be.calls if c[0] == "set_order"]) == n


@pytest.mark.asyncio
async def test_offline_refuses_every_manage_key_and_the_footer_shows_the_warm_cap_live():
    be = FakeBackend(mode="offline", roster=[{"name": "alpha", "id": "alpha-1", "port": 7901, "pid": None, "running": False}])
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        for action in ("new_member", "rename", "delete", "add_remote", "edit_remote", "move_down", "move_up"):
            assert app.screen.check_action(action, ()) is False, action  # hidden and inert offline
        for key in ("n", "R", "d", "a", "e", "J"):
            await pilot.press(key)
            await pilot.pause(0.1)
            assert isinstance(app.screen, RosterScreen), key
        assert not [c for c in be.calls if c[0] in ("create", "rename", "remove", "remote_add", "remote_update", "remote_remove", "set_order")]
        assert "warm cap" not in str(app.screen.query_one("#status", Static).content)
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        assert await _until(pilot, lambda: "warm cap 3" in str(app.screen.query_one("#status", Static).content))


@pytest.mark.asyncio
async def test_the_footer_shows_each_manage_key_only_where_it_applies():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        scr = app.screen
        table = scr.query_one("#roster", DataTable)
        table.move_cursor(row=0)  # host
        await pilot.pause(0.1)
        assert {a: scr.check_action(a, ()) for a in ("new_member", "add_remote", "rename", "delete", "edit_remote", "move_down")} == {"new_member": True, "add_remote": True, "rename": False, "delete": False, "edit_remote": False, "move_down": True}
        table.move_cursor(row=1)  # a local member
        await pilot.pause(0.1)
        assert (scr.check_action("rename", ()), scr.check_action("delete", ()), scr.check_action("edit_remote", ())) == (True, True, False)
        table.move_cursor(row=4)  # the remote
        await pilot.pause(0.1)
        assert (scr.check_action("rename", ()), scr.check_action("delete", ()), scr.check_action("edit_remote", ())) == (True, True, True)


@pytest.mark.asyncio
async def test_every_manage_modal_holds_inside_the_decks_80x24_floor():
    """A refused submit must show its hint and the buttons must be on screen at the deck's
    smallest supported terminal."""
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        for key, kind in (("n", NewAgentModal), ("a", RemoteModal), ("R", RenameModal), ("d", DeleteModal)):
            if kind is not NewAgentModal and kind is not RemoteModal:
                app.screen.query_one("#roster", DataTable).move_cursor(row=1)
                await pilot.pause(0.1)
            await pilot.press(key)
            assert await _until(pilot, lambda: isinstance(app.screen, kind)), key
            modal = app.screen
            await pilot.pause(0.2)
            box = modal.query_one(".manage-box")
            submit, hint = modal.query_one("#submit", Button), modal.query_one("#hint", Static)
            assert box.region.height <= 24 and submit.region.y + submit.region.height <= 24 and 0 <= hint.region.y < 24, (kind.__name__, box.region, submit.region, hint.region)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert isinstance(app.screen, RosterScreen)
