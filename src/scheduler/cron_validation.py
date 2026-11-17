"""Detect croniter 7 max/step expressions without changing stored schedules."""

from __future__ import annotations

import re

# croniter's default order: minute, hour, day, month, weekday, second, year.
_FIELDS = (
    ({"59"}, "minute", "minutes"),
    ({"23"}, "hour", "hours"),
    ({"31"}, "day-of-month", "days"),
    ({"12", "DEC"}, "month", "months"),
    ({"6", "7", "SAT"}, "day-of-week", "days of the week"),
    ({"59"}, "second", "seconds"),
    ({"2099"}, "year", "years"),
)


def cron_ambiguity_problem(expression: str) -> str | None:
    """Return rewrite hints for max/step list terms in 5-, 6-, or 7-field cron.

    This is an ambiguity guard, not a syntax validator. Explicit ranges and
    wildcard steps retain their meaning. Seconds are trailing, not leading.
    """
    fields = expression.split()
    if len(fields) not in (5, 6, 7):
        return None
    problems = []
    for field, (maxima, unit, plural) in zip(fields, _FIELDS):
        for term in field.split(","):
            match = re.fullmatch(r"([A-Za-z]+|[0-9]+)/([0-9]+)", term)
            if not match:
                continue
            start, step = match.groups()
            normalized = str(int(start)) if start.isdigit() else start.upper()
            if normalized in maxima:
                problems.append(
                    f"`{term}` is ambiguous in cron; use `*/{step}` for every {step} {plural}, "
                    f"or `{start}` for {unit} {start} only."
                )
    return " ".join(problems) or None
