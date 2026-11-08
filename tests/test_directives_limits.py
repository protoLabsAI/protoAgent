"""Limits on the directive ledger (ADR 0127 D6, #4854 review minors c–d).

Two bounds that stop a ledger from quietly dominating a turn:

* the ``STANDING DIRECTIVES`` section of ``<working_state>`` is capped by a *total*
  character budget, not just an entry count — 20 maximum-size directives would otherwise
  render ~160K chars every turn that never shed; and
* the four directive tools are always kept out of deferral, even behind a configured keep
  override, so an approval can be recorded the moment it is given.
"""

from __future__ import annotations

import pytest

import runtime.state as rs
from graph.directives import (
    MAX_NOTE_CHARS,
    MAX_QUOTE_CHARS,
    WS_DIRECTIVE_CAP,
    WS_DIRECTIVES_MAX_CHARS,
    WS_HEADING,
    DirectiveStore,
    working_state_section,
)


@pytest.fixture
def store(tmp_path, monkeypatch) -> DirectiveStore:
    s = DirectiveStore(tmp_path / "directives")
    monkeypatch.setattr(rs.STATE, "directive_store", s, raising=False)
    return s


# ── char budget for the working-state section ──────────────────────────────────────


def test_section_stays_within_budget_with_a_pointer(store):
    """20 maximum-size directives (each alone over budget) render within the budget plus a
    ``…+N more`` pointer, with the right N — not ~160K chars of unshed text."""
    for _ in range(WS_DIRECTIVE_CAP):
        store.record(
            "s",
            "A" * MAX_QUOTE_CHARS,
            conditions="B" * MAX_NOTE_CHARS,
            scope="C" * MAX_NOTE_CHARS,
        )
    section = working_state_section("s")

    assert WS_HEADING in section
    # Only the first entry fits; it alone is over budget, so it renders clipped, not dropped.
    assert "…[clipped," in section
    assert f"…+{WS_DIRECTIVE_CAP - 1} more (/directives to list them)" in section
    # The naive render is ~160K chars; the budget keeps it an order of magnitude smaller.
    naive = WS_DIRECTIVE_CAP * (MAX_QUOTE_CHARS + 2 * MAX_NOTE_CHARS)
    assert naive > 150_000
    assert len(section) < WS_DIRECTIVES_MAX_CHARS + 1000


def test_whole_entries_render_oldest_first_until_the_budget(store):
    """Entries render whole, oldest first, until the next would exceed the budget; the rest
    become the pointer. No clip marker when several whole entries fit."""
    for i in range(WS_DIRECTIVE_CAP):
        store.record("s", f"rule {i} " + "x" * 1500)
    section = working_state_section("s")

    assert "rule 0 " in section  # the first (oldest) entry always renders
    assert "more (/directives to list them)" in section
    assert "…[clipped," not in section  # whole entries only — nothing is clipped here
    assert len(section) < WS_DIRECTIVES_MAX_CHARS + 1000
    # the budget bites before the entry cap: not all 20 fit in 6000 chars
    assert f"rule {WS_DIRECTIVE_CAP - 1} " not in section


def test_a_single_small_directive_renders_in_full_with_no_pointer(store):
    """The budget never touches an ordinary ledger: one short directive renders verbatim
    with no clip and no pointer."""
    store.record("s", "cut the release once #A and #B merge; stop on a WARN or FAIL")
    section = working_state_section("s")

    assert "cut the release once #A and #B merge; stop on a WARN or FAIL" in section
    assert "…[clipped," not in section
    assert "more (" not in section


# ── directive tools are never deferred ──────────────────────────────────────────────


def test_resolve_deferred_keep_always_keeps_directive_tools():
    """A configured keep override can't defer the directive tools or ``search_tools``."""
    from tools.directive_tools import DIRECTIVE_TOOL_NAMES
    from tools.lg_tools import SEARCH_TOOLS_NAME, resolve_deferred_keep

    keep = resolve_deferred_keep(["calculator"])

    assert "calculator" in keep  # the override is honoured for everything else
    assert SEARCH_TOOLS_NAME in keep
    assert DIRECTIVE_TOOL_NAMES <= keep
    assert len(DIRECTIVE_TOOL_NAMES) == 4
