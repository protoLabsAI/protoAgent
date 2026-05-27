"""Tests for evals.compare — A/B diff of two eval reports."""

from evals.compare import compare_reports


def _report(results):
    return {"total": len(results), "passed": sum(1 for r in results if r["passed"]),
            "results": results}


def test_overall_and_flips():
    old = _report([
        {"id": "a", "category": "tool", "name": "A", "passed": True, "detail": ""},
        {"id": "b", "category": "tool", "name": "B", "passed": False, "detail": ""},
        {"id": "c", "category": "a2a", "name": "C", "passed": True, "detail": ""},
    ])
    new = _report([
        {"id": "a", "category": "tool", "name": "A", "passed": True, "detail": ""},
        {"id": "b", "category": "tool", "name": "B", "passed": True, "detail": ""},   # fixed
        {"id": "c", "category": "a2a", "name": "C", "passed": False, "detail": ""},   # regressed
    ])
    md = compare_reports(old, new)
    assert "2/3" in md and "1/3" not in md.split("\n")[2]  # overall line: 2/3 → 2/3
    assert "Newly passing" in md and "`b`" in md
    assert "Newly failing" in md and "`c`" in md
    # per-category table present
    assert "| tool |" in md and "| a2a |" in md


def test_no_flips_message():
    rep = _report([{"id": "a", "category": "x", "name": "A", "passed": True, "detail": ""}])
    md = compare_reports(rep, rep)
    assert "No cases flipped" in md


def test_handles_new_and_removed_categories():
    old = _report([{"id": "a", "category": "old_cat", "name": "A", "passed": True, "detail": ""}])
    new = _report([{"id": "b", "category": "new_cat", "name": "B", "passed": True, "detail": ""}])
    md = compare_reports(old, new)
    assert "old_cat" in md and "new_cat" in md
