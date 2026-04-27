"""Tests for the starter tools in ``tools/lg_tools.py``.

These tools ship with the template so a fresh clone can demonstrate
real tool-calling behaviour. The assertions here lock in the
guarantees forks will lean on — safe eval for the calculator,
error-string (not raise) semantics on invalid input, and tz lookup
on current_time.

Network-dependent tools (``web_search`` / ``fetch_url``) are covered
by offline error-path tests only — we don't hit the live internet
in CI.
"""

from __future__ import annotations

import pytest


# ── calculator ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("expr,expected", [
    ("1 + 2", 3),
    ("2 * 3 + 4", 10),
    ("(1 + 2) * 3", 9),
    ("2 ** 10", 1024),
    ("10 // 3", 3),
    ("10 % 3", 1),
    ("-5 + 3", -2),
    ("100 / 4", 25.0),
])
@pytest.mark.asyncio
async def test_calculator_happy_path(expr, expected):
    from tools.lg_tools import calculator
    result = await calculator.ainvoke({"expression": expr})
    assert str(expected) in result


@pytest.mark.asyncio
async def test_calculator_rejects_names():
    """The safe evaluator must refuse identifiers — no smuggling
    in ``__import__`` or any other attribute access via ``eval``."""
    from tools.lg_tools import calculator
    result = await calculator.ainvoke({"expression": "__import__('os').system('ls')"})
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_calculator_rejects_function_calls():
    from tools.lg_tools import calculator
    result = await calculator.ainvoke({"expression": "abs(-5)"})
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_calculator_rejects_attribute_access():
    from tools.lg_tools import calculator
    result = await calculator.ainvoke({"expression": "(1).__class__"})
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_calculator_handles_divide_by_zero():
    from tools.lg_tools import calculator
    result = await calculator.ainvoke({"expression": "1 / 0"})
    assert "division by zero" in result.lower()


@pytest.mark.asyncio
async def test_calculator_handles_syntax_error():
    from tools.lg_tools import calculator
    result = await calculator.ainvoke({"expression": "1 +"})
    assert result.startswith("Error:")


# ── current_time ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_current_time_utc_default():
    from tools.lg_tools import current_time
    result = await current_time.ainvoke({})
    assert "(UTC)" in result
    assert "Human:" in result


@pytest.mark.asyncio
async def test_current_time_named_zone():
    from tools.lg_tools import current_time
    result = await current_time.ainvoke({"timezone": "America/New_York"})
    assert "(America/New_York)" in result


@pytest.mark.asyncio
async def test_current_time_unknown_zone_returns_error():
    from tools.lg_tools import current_time
    result = await current_time.ainvoke({"timezone": "Not/A_Zone"})
    assert result.startswith("Error:")


# ── fetch_url — offline error paths only ─────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_scheme():
    """Guard against file:// / javascript: / etc. — never fetch local
    files, never evaluate data-uri-flavoured inputs."""
    from tools.lg_tools import fetch_url
    for bad in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/",
        "not-a-url",
    ):
        result = await fetch_url.ainvoke({"url": bad})
        assert result.startswith("Error:"), f"accepted unsafe url: {bad!r}"
