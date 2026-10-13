"""One base for the deck's modals: a modal answers ONCE.

Textual's ``Screen.dismiss`` has no is-active guard and ``App.pop_screen`` pops whatever is
on top, so a second ``Input.Submitted`` / ``Button.Pressed`` already queued behind the
first (a double click, Enter twice at driver speed) would run the callback's mutation
once but pop the screen UNDER the modal — the roster — leaving a blank deck. Every deck
modal dismisses through :meth:`OnceModal.dismiss`, which lets the first answer through and
drops the rest.
"""

from __future__ import annotations

from typing import Any

from textual.screen import ModalScreen, ScreenResultType


class OnceModal(ModalScreen[ScreenResultType]):
    """A ModalScreen whose ``dismiss`` is idempotent: the first call answers, later calls
    (a queued duplicate submit, a cancel racing a submit) are ignored."""

    _answered = False

    def dismiss(self, result: Any = None):  # type: ignore[override]
        if self._answered or not self.is_active:
            return None
        self._answered = True
        return super().dismiss(result)
