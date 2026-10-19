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


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("1 + 2", 3),
        ("2 * 3 + 4", 10),
        ("(1 + 2) * 3", 9),
        ("2 ** 10", 1024),
        ("10 // 3", 3),
        ("10 % 3", 1),
        ("-5 + 3", -2),
        ("100 / 4", 25.0),
    ],
)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", ["2 ** 1000000000000", "2 ** -1000000000000", "999999999999999999 ** 4096"])
async def test_calculator_refuses_expensive_power_before_calling_operator(monkeypatch, expression):
    import ast

    from tools import lg_tools

    calls = []

    def record_power(left, right):
        calls.append((left, right))
        return 0  # never evaluate an unbounded power even if the regression returns

    monkeypatch.setitem(lg_tools._BIN_OPS, ast.Pow, record_power)
    result = await lg_tools.calculator.ainvoke({"expression": expression})
    assert result.startswith("Error:") and "limit" in result
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", [
    "9" * 1400,  # large literal, before any operators
    "(2 ** 2000) * (2 ** 2000) * (2 ** 2000)",  # oversized intermediate
    "1+" * 300 + "1",  # too many AST nodes
    "-" * 100 + "1",  # too much recursion even with few nodes
    " " * 5000 + "1",  # parser input budget
], ids=["literal", "intermediate", "node-count", "depth", "input-length"])
async def test_calculator_bounds_input_and_intermediate_work(expression):
    from tools.lg_tools import calculator

    result = await calculator.ainvoke({"expression": expression})
    assert result.startswith("Error:") and "limit" in result


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


def _mock_httpx(monkeypatch, handler):
    """Route fetch_url's AsyncClient through an in-process transport."""
    import httpx

    real = httpx.AsyncClient

    class _Client(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_fetch_url_reports_a_redirect_loop_instead_of_the_30x_body(monkeypatch):
    """Past the hop cap the last response is still a redirect; its stub body used to
    come back as the page's content under a `[302]` header."""
    import httpx

    from tools.lg_tools import fetch_url

    def _loop(request):
        n = int(request.url.params.get("n", "0"))
        return httpx.Response(302, headers={"location": f"/r?n={n + 1}"}, text="Moved")

    _mock_httpx(monkeypatch, _loop)
    result = await fetch_url.ainvoke({"url": "https://example.com/r"})
    assert result.startswith("Error: too many redirects"), result


@pytest.mark.asyncio
async def test_fetch_url_follows_a_short_redirect_chain(monkeypatch):
    import httpx

    from tools.lg_tools import fetch_url

    def _handler(request):
        if request.url.path == "/final":
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="the page")
        return httpx.Response(301, headers={"location": "/final"})

    _mock_httpx(monkeypatch, _handler)
    result = await fetch_url.ainvoke({"url": "https://example.com/start"})
    assert result.startswith("[200]") and "the page" in result, result


@pytest.mark.asyncio
async def test_current_time_survives_a_missing_tz_database(monkeypatch):
    """Windows ships no IANA data (stdlib zoneinfo needs the bundled tzdata
    package, #1683): with NO database, every name — including the tool's own
    docstring examples — raised, and the tool errored on every call. It must
    instead answer in UTC, noting the unavailable zone for named requests."""
    import tools.lg_tools as lg

    class _NoDb:
        def __init__(self, name):
            raise lg.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(lg, "ZoneInfo", _NoDb)

    default = await lg.current_time.ainvoke({})
    assert not default.startswith("Error:")
    assert "(UTC)" in default and "Human:" in default
    assert "Note:" not in default  # UTC asked, UTC answered — nothing to caveat

    named = await lg.current_time.ainvoke({"timezone": "America/New_York"})
    assert not named.startswith("Error:")
    assert "(UTC)" in named
    assert "no IANA timezone database" in named


@pytest.mark.asyncio
async def test_fetch_url_truncation_marker_reports_total_and_remedy(monkeypatch):
    """A bare "[truncated]" let the model blame the page ("JS-rendered") when the cap
    cut real content; the marker must say how much was shown and how to read the rest."""
    import httpx

    from tools.lg_tools import fetch_url

    body = "<html><body><main>" + "x" * 500 + "</main></body></html>"
    _mock_httpx(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=body))

    cut = await fetch_url.ainvoke({"url": "https://example.com/", "max_chars": 100})
    assert "…[truncated: showing 100 of 500 chars" in cut
    assert "larger max_chars" in cut

    full = await fetch_url.ainvoke({"url": "https://example.com/", "max_chars": 1000})
    assert "truncated" not in full
