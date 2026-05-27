"""Tests for the with_fallback tool decorator."""

import pytest

from tools.fallbacks import with_fallback


@pytest.mark.asyncio
async def test_async_success_passthrough():
    @with_fallback()
    async def ok(x: str) -> str:
        return f"got {x}"

    assert await ok("hi") == "got hi"


@pytest.mark.asyncio
async def test_async_exception_becomes_partial_result():
    @with_fallback()
    async def boom(x: str) -> str:
        raise ValueError("kaboom")

    result = await boom("hi")
    assert result.startswith("Error (partial result):")
    assert "ValueError: kaboom" in result
    # Default detail names the wrapped function.
    assert "boom" in result


@pytest.mark.asyncio
async def test_custom_fallback_message():
    @with_fallback("the weather service is down")
    async def boom() -> str:
        raise RuntimeError("503")

    result = await boom()
    assert "the weather service is down" in result
    assert "RuntimeError: 503" in result


def test_sync_tool_supported():
    @with_fallback()
    def boom() -> str:
        raise KeyError("missing")

    result = boom()
    assert result.startswith("Error (partial result):")
    assert "KeyError" in result


def test_sync_success_passthrough():
    @with_fallback()
    def ok() -> str:
        return "fine"

    assert ok() == "fine"


@pytest.mark.asyncio
async def test_long_error_is_truncated():
    @with_fallback()
    async def boom() -> str:
        raise ValueError("x" * 1000)

    result = await boom()
    # Error message clamped to 200 chars.
    assert "x" * 200 in result
    assert "x" * 300 not in result


@pytest.mark.asyncio
async def test_preserves_name_and_signature():
    @with_fallback()
    async def my_named_tool(query: str, limit: int = 5) -> str:
        """Docstring stays."""
        return query

    import inspect

    assert my_named_tool.__name__ == "my_named_tool"
    assert my_named_tool.__doc__ == "Docstring stays."
    # Signature is recoverable through functools.wraps __wrapped__.
    params = list(inspect.signature(my_named_tool).parameters)
    assert params == ["query", "limit"]
