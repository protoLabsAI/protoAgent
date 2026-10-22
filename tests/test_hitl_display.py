"""HITL ``display`` validation + ``_interrupt_payload`` passthrough (ADR 0118, Refs #4405).

The server half: an optional ``display={component:'table', props:{columns, rows}}`` on a
HITL interrupt is validated host-side and carried through ``server/chat.py::_interrupt_payload``
as inert render data. An invalid/unsupported/oversized one is dropped WITHOUT changing the
pause's prose, options or resume value, and the caller's payload is never mutated. These tests
exercise the real ``server._interrupt_payload`` seam plus the ``hitl_display`` validator's
size/shape boundaries.
"""

from __future__ import annotations

import copy

import pytest

import server
from server import hitl_display


def _table(columns, rows):
    return {"component": "table", "props": {"columns": list(columns), "rows": [list(r) for r in rows]}}


# ── valid table rides through every dict path, input untouched (r1) ───────────────────────


def test_valid_table_rides_through_a_question():
    val = {"question": "Which row?", "display": _table(["Name", "Age"], [["Ada", 36], ["Alan", 41]])}
    original = copy.deepcopy(val)
    out = server._interrupt_payload(val)
    assert out["question"] == "Which row?"
    assert out["display"] == _table(["Name", "Age"], [["Ada", 36], ["Alan", 41]])
    assert val == original  # the caller's payload is copied, never mutated
    # The carried display is a fresh copy, not an alias into the input.
    assert out["display"] is not val["display"]
    assert out["display"]["props"]["rows"] is not val["display"]["props"]["rows"]


def test_valid_table_rides_through_a_form_and_preserves_steps():
    val = {
        "kind": "form",
        "title": "Pick env",
        "description": "Confirm the target.",
        "steps": [{"schema": {"type": "object"}}],
        "display": _table(["Env"], [["staging"], ["prod"]]),
    }
    original = copy.deepcopy(val)
    out = server._interrupt_payload(val)
    assert out["kind"] == "form"
    assert out["title"] == "Pick env"
    assert out["description"] == "Confirm the target."
    assert out["steps"] == [{"schema": {"type": "object"}}]
    assert out["display"] == _table(["Env"], [["staging"], ["prod"]])
    assert val == original


def test_valid_table_rides_through_an_approval_preserving_detail_and_options():
    val = {
        "kind": "approval",
        "title": "Run migration",
        "detail": "alembic upgrade head",
        "options": ["approve", "deny"],
        "display": _table(["Step", "SQL"], [["upgrade", "CREATE TABLE …"]]),
    }
    original = copy.deepcopy(val)
    out = server._interrupt_payload(val)
    # The approval's decision contract — kind/title/detail/options — is unchanged.
    assert out["kind"] == "approval"
    assert out["title"] == "Run migration"
    assert out["detail"] == "alembic upgrade head"
    assert out["options"] == ["approve", "deny"]
    assert out["display"] == _table(["Step", "SQL"], [["upgrade", "CREATE TABLE …"]])
    assert val == original


def test_valid_table_rides_through_an_unrecognised_prose_dict():
    val = {"type": "x_handoff", "prompt": "Do this.", "title": "Heading", "display": _table(["A"], [[1]])}
    out = server._interrupt_payload(val)
    assert out["question"] == "Do this."
    assert out["title"] == "Heading"
    assert out["display"] == _table(["A"], [[1]])


def test_scalar_cell_variety_is_accepted():
    disp = _table(["s", "i", "f", "b", "n"], [["x", 1, 3.14, True, None], ["y", -2, 0.0, False, None]])
    out = server._interrupt_payload({"question": "ok?", "display": disp})
    assert out["display"] == disp


# ── legacy payloads are byte-for-byte unchanged (r3) ──────────────────────────────────────


def test_legacy_form_payload_identity_preserved():
    # No `display` key → returned verbatim (same object), exactly as before this change.
    val = {"kind": "form", "title": "Pick", "description": "", "steps": [{"schema": {}}]}
    assert server._interrupt_payload(val) is val


def test_legacy_question_and_approval_unchanged():
    assert server._interrupt_payload({"question": "Merge it?"}) == {"question": "Merge it?"}
    appr = {"kind": "approval", "title": "Run it", "detail": "rm -rf /tmp/x", "options": ["approve", "deny"]}
    assert server._interrupt_payload(appr) is appr


def test_plain_values_unchanged():
    assert server._interrupt_payload("just text") == {"question": "just text"}
    assert server._interrupt_payload(None) == {"question": "Input required."}
    out = server._interrupt_payload({"foo": 1})
    assert "question" in out and '"foo": 1' in out["question"]
    assert "display" not in out


# ── invalid/unsupported display is dropped; the pause stays answerable (r2) ────────────────

_HUGE_CELL = "x" * (hitl_display.MAX_CELL_CHARS + 1)
# A shape that clears every dimension/cell limit but blows the serialized-byte ceiling.
_OVERSIZE_BYTES_TABLE = _table(
    [f"c{i}" for i in range(hitl_display.MAX_COLUMNS)],
    [["x" * hitl_display.MAX_CELL_CHARS] * hitl_display.MAX_COLUMNS for _ in range(hitl_display.MAX_ROWS)],
)

_BAD_DISPLAYS = {
    "not_a_dict": "nope",
    "unknown_component": {"component": "keyvalue", "props": {"columns": ["a"], "rows": [["1"]]}},
    "custom_component": {"component": "my-plugin-widget", "props": {"columns": ["a"], "rows": [["1"]]}},
    "props_not_object": {"component": "table", "props": "nope"},
    "missing_columns": {"component": "table", "props": {"rows": [["1"]]}},
    "missing_rows": {"component": "table", "props": {"columns": ["a"]}},
    "columns_not_list": {"component": "table", "props": {"columns": "a,b", "rows": []}},
    "non_string_header": {"component": "table", "props": {"columns": ["ok", 7], "rows": []}},
    "ragged_row": {"component": "table", "props": {"columns": ["a", "b"], "rows": [["1"]]}},
    "non_scalar_cell": {"component": "table", "props": {"columns": ["a"], "rows": [[{"nested": 1}]]}},
    "list_cell": {"component": "table", "props": {"columns": ["a"], "rows": [[[1, 2]]]}},
    "nan_cell": {"component": "table", "props": {"columns": ["a"], "rows": [[float("nan")]]}},
    "inf_cell": {"component": "table", "props": {"columns": ["a"], "rows": [[float("inf")]]}},
    "too_many_columns": {
        "component": "table",
        "props": {"columns": [f"c{i}" for i in range(hitl_display.MAX_COLUMNS + 1)], "rows": []},
    },
    "too_many_rows": {
        "component": "table",
        "props": {"columns": ["a"], "rows": [["r"] for _ in range(hitl_display.MAX_ROWS + 1)]},
    },
    "oversize_header": {"component": "table", "props": {"columns": ["x" * (hitl_display.MAX_HEADER_CHARS + 1)], "rows": []}},
    "oversize_cell": {"component": "table", "props": {"columns": ["a"], "rows": [[_HUGE_CELL]]}},
    "oversize_bytes": _OVERSIZE_BYTES_TABLE,
}


@pytest.mark.parametrize("name", sorted(_BAD_DISPLAYS))
def test_invalid_display_dropped_but_pause_survives(name):
    bad = _BAD_DISPLAYS[name]
    val = {"question": "Pick one", "options": ["a", "b"], "display": bad}
    original = copy.deepcopy(val)
    out = server._interrupt_payload(val)
    # Only `display` is dropped — prose, options and the rest stay intact and answerable.
    assert out["question"] == "Pick one"
    assert out["options"] == ["a", "b"]
    assert "display" not in out
    assert val == original  # input never mutated, even on the reject path


def test_invalid_display_on_approval_keeps_decision_contract():
    val = {
        "kind": "approval",
        "title": "Run migration",
        "detail": "alembic upgrade head",
        "options": ["approve", "deny"],
        "display": {"component": "table", "props": {"columns": ["a", "b"], "rows": [["only-one"]]}},  # ragged
    }
    out = server._interrupt_payload(val)
    assert out["kind"] == "approval"
    assert out["title"] == "Run migration"
    assert out["detail"] == "alembic upgrade head"
    assert out["options"] == ["approve", "deny"]
    assert "display" not in out


def test_display_excluded_from_prose_fallback():
    # A dict with no prompt/message/text degrades to a JSON question; a (would-be) display
    # must never leak into that prose, whether it validates or not.
    out = server._interrupt_payload({"foo": 1, "display": _table(["A"], [[1]])})
    assert '"foo": 1' in out["question"]
    assert "component" not in out["question"] and "table" not in out["question"]
    assert out["display"] == _table(["A"], [[1]])


# ── validator boundaries, exercised directly (r4) ─────────────────────────────────────────


def test_validator_returns_fresh_copy_of_vetted_fields_only():
    src = {"component": "table", "props": {"columns": ["A"], "rows": [[1]], "extra": "ignored"}, "stray": 1}
    out = hitl_display.validate_display(src)
    assert out == {"component": "table", "props": {"columns": ["A"], "rows": [[1]]}}  # extras not copied
    assert out["props"]["rows"] is not src["props"]["rows"]  # fresh, not aliased


def test_validator_accepts_at_the_limits():
    at_limit = _table(
        [f"c{i}" for i in range(hitl_display.MAX_COLUMNS)],
        [["x" * 4] * hitl_display.MAX_COLUMNS for _ in range(hitl_display.MAX_ROWS)],
    )
    assert hitl_display.validate_display(at_limit) == at_limit


def test_validator_rejects_oversize_bytes_within_shape_limits():
    assert hitl_display.validate_display(_OVERSIZE_BYTES_TABLE) is None


def test_validator_rejects_non_finite_and_ragged():
    assert hitl_display.validate_display(_table(["a"], [[float("nan")]])) is None
    assert hitl_display.validate_display(_table(["a"], [[float("-inf")]])) is None
    assert hitl_display.validate_display({"component": "table", "props": {"columns": ["a", "b"], "rows": [[1]]}}) is None


def test_validator_none_for_non_table_and_non_dict():
    assert hitl_display.validate_display(None) is None
    assert hitl_display.validate_display("x") is None
    assert hitl_display.validate_display({"component": "keyvalue", "props": {"columns": [], "rows": []}}) is None


def test_empty_table_is_valid():
    assert hitl_display.validate_display(_table([], [])) == {"component": "table", "props": {"columns": [], "rows": []}}
