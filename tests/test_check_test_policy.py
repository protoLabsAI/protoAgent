"""The test-policy gate's arithmetic (``scripts/check_test_policy.py``, docs/dev/testing.md).

The gate itself runs against the real tree in ``scripts/gate.py``; this pins only the
two decisions a wrong edit would silently break: what counts as over, and that
``--tighten`` can never loosen a ceiling.
"""

from __future__ import annotations

from scripts import check_test_policy as policy


def test_a_metric_over_or_missing_its_ceiling_fails_and_one_at_it_passes():
    actual = {"web_e2e_tests": 10, "py_real_sleeps": 3, "e2e_fixed_waits": 0}
    failed = policy.violations(actual, {"web_e2e_tests": 10, "py_real_sleeps": 2})
    assert [m.split(":")[0] for m in failed] == ["py_real_sleeps", "e2e_fixed_waits"]
    assert "virtual_clock" in failed[0]  # the message carries the remedy


def test_tighten_lowers_ceilings_and_never_raises_one():
    actual = {"py_test_lines_per_source_line": 1.2345, "web_e2e_tests": 12, "py_real_sleeps": 1}
    ceilings = {"py_test_lines_per_source_line": 1.5, "web_e2e_tests": 10, "py_real_sleeps": 5}
    assert policy.tightened(actual, ceilings) == {
        "py_test_lines_per_source_line": 1.24,
        "web_e2e_tests": 10,
        "py_real_sleeps": 1,
    }
