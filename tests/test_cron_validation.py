"""Field-aware croniter upgrade guard, including trailing seconds and year."""

import pytest

from scheduler.cron_validation import cron_ambiguity_problem


@pytest.mark.parametrize("count, index, term, unit", [
    (count, index, term, unit)
    for count in (5, 6, 7)
    for index, term, unit in [
    (0, "59/15", "minutes"), (1, "23/6", "hours"),
    (2, "31/5", "days"), (3, "12/3", "months"),
    (3, "DEC/3", "months"), (3, "dec/3", "months"),
    (4, "6/1", "days of the week"), (4, "SAT/2", "days of the week"),
    (4, "7/2", "days of the week"), (5, "59/15", "seconds"),
    (6, "2099/1", "years"), (0, "059/15", "minutes"),
    ] if index < count
])
def test_max_step_rewrite(count, index, term, unit):
    fields = ["*"] * count
    fields[index] = term
    problem = cron_ambiguity_problem(" ".join(fields))
    start, step = term.split("/")
    assert f"`{term}` is ambiguous" in problem
    assert f"`*/{step}` for every {step} {unit}" in problem
    assert f"or `{start}`" in problem


@pytest.mark.parametrize("expression", [
    "*/15 * * * *", "0 9 * * 1", "15 7,12 * * *", "23/6 * * * *",
    "0-59/15 * * * *", "59 * * * *", "0 0 1 JAN/3 SUN/2",
    "0 0 * * * 0-59/15", "0 0 * * * 0 2026/2", "@daily",
    "2099-01-01T00:00:00+00:00",
])
def test_unambiguous_expressions_unchanged(expression):
    assert cron_ambiguity_problem(expression) is None


def test_list_terms_and_multiple_fields():
    problem = cron_ambiguity_problem("0,59/15 23/6 * * *")
    assert "`59/15`" in problem
    assert "`23/6`" in problem
