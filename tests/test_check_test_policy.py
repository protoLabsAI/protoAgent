"""The test-policy gate's arithmetic (``scripts/check_test_policy.py``, docs/dev/testing.md).

The gate itself runs against the real tree in ``scripts/gate.py``; this pins the
decisions a wrong edit would silently break: what a change is charged for, that a small
fix always fits, and that ``--tighten`` can never loosen a burndown.
"""

from __future__ import annotations

import pytest

from scripts import check_test_policy as policy

LIMITS = {
    "free_test_lines": 250,
    "py_test_lines_per_source_line": 2.0,
    "web_unit_test_lines_per_source_line": 1.5,
    "e2e_tests": 5,
}


def _delta(**over: int) -> dict[str, int]:
    return {"py_test": 0, "py_source": 0, "web_test": 0, "web_source": 0, "e2e_tests": 0} | over


@pytest.mark.parametrize(
    ("delta", "over"),
    [
        (_delta(py_test=250, py_source=3), []),  # a small fix with its tests: free allowance
        (_delta(py_test=1000, py_source=500), []),  # at 2x source
        (_delta(py_test=582), ["Python"]),  # test-only, past the free allowance
        (_delta(py_test=588, py_source=192), ["Python"]),  # 3x source
        (_delta(py_test=300, py_source=-40), ["Python"]),  # removed source earns no budget
        (_delta(web_test=299, web_source=89), ["console unit"]),
        (_delta(e2e_tests=6), ["Playwright"]),
        (_delta(py_test=-400, web_test=-10, e2e_tests=-2), []),  # deleting is always fine
    ],
)
def test_a_change_is_charged_only_for_its_own_diff(delta, over):
    messages = policy.change_violations(delta, LIMITS)
    assert len(messages) == len(over)
    assert all(label in message for label, message in zip(over, messages))


def test_the_message_says_how_much_to_cut():
    (message,) = policy.change_violations(_delta(py_test=588, py_source=192), LIMITS)
    assert "budget is 384" in message and "cut 204" in message


def test_burndowns_fail_over_their_ceiling_and_tighten_never_raises_one():
    actual = {"web_e2e_tests": 999, "py_real_sleeps": 3, "e2e_fixed_waits": 1}
    ceilings = {"py_real_sleeps": 2, "e2e_fixed_waits": 5}
    assert [m.split(":")[0] for m in policy.violations(actual, ceilings)] == ["py_real_sleeps"]
    assert policy.tightened(actual, ceilings) == {"py_real_sleeps": 2, "e2e_fixed_waits": 1}
