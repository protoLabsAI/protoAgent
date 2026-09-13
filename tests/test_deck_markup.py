"""Member- and hub-authored text reaches the deck's widgets as rich Text, never as console
markup: Textual parses a plain str, and a stray bracket (`[/]`, an unclosed `[bold`) raises
MarkupError on the UI thread — for a HITL modal that means the operator cannot answer the
parked turn at all (CodeRabbit on the epic, #3491)."""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Label, Static

from deck.app import FleetDeck
from deck.hitl import ApprovalModal, FormModal, QuestionModal
from tests.test_deck_app import FakeBackend, _settle

EVIL = "Coach [/] [bold [link=x] ]["


class _Host(App):
    def __init__(self, modal) -> None:
        super().__init__()
        self._modal = modal

    def on_mount(self) -> None:
        self.push_screen(self._modal)


def _texts(screen) -> str:
    return "\n".join(str(w.content) for w in screen.query(Static)) + "\n".join(str(w.content) for w in screen.query(Label))


_FORM = {
    "kind": "form",
    "title": EVIL,
    "steps": [
        {
            "title": EVIL,
            "description": EVIL,
            "schema": {
                "properties": {
                    "a": {"type": "string", "title": EVIL, "description": EVIL},
                    "b": {"type": "string", "title": EVIL, "enum": ["x", EVIL]},
                    "c": {"type": "array", "title": EVIL, "items": {"type": "string", "enum": [EVIL, "y"]}},
                    "d": {"type": "boolean", "title": EVIL},
                },
                "required": ["a", EVIL],
            },
        },
        {"title": EVIL, "schema": {"properties": {"e": {"type": "string", "title": EVIL}}}},
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make",
    [
        lambda: ApprovalModal(EVIL, {"kind": "approval", "title": EVIL, "detail": EVIL, "project": EVIL}),
        lambda: QuestionModal(EVIL, {"question": EVIL}),
        lambda: FormModal(EVIL, _FORM),
    ],
    ids=["approval", "question", "form"],
)
async def test_member_authored_hitl_text_renders_literally(make):
    app = _Host(make())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause(0.3)
        assert "[/]" in _texts(app.screen)  # shown as typed, not parsed


@pytest.mark.asyncio
async def test_hub_authored_banner_and_status_render_literally():
    be = FakeBackend(warnings=[EVIL])
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        assert EVIL in str(app.screen.query_one("#banner", Static).content)
