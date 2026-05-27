"""Tests for the enforcement gate (RateLimiter + EnforcementMiddleware)."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from enforcement.rate_limiter import RateLimiter
from graph.middleware.enforcement import EnforcementMiddleware


def _req(name, args=None, call_id="c1"):
    return SimpleNamespace(tool_call={"name": name, "args": args or {}, "id": call_id})


# ── RateLimiter ───────────────────────────────────────────────────────────────

def test_rate_limiter_unlimited_when_unconfigured():
    rl = RateLimiter()
    for _ in range(100):
        assert rl.check("anything") == (True, None)


def test_rate_limiter_blocks_over_limit():
    rl = RateLimiter({"x": {"max": 2, "window_seconds": 60}})
    assert rl.check("x")[0] is True
    assert rl.check("x")[0] is True
    allowed, reason = rl.check("x")
    assert allowed is False and "Rate limit exceeded" in reason
    # A different action is independent.
    assert rl.check("y")[0] is True


def test_rate_limiter_reset():
    rl = RateLimiter({"x": {"max": 1, "window_seconds": 60}})
    assert rl.check("x")[0] is True
    assert rl.check("x")[0] is False
    rl.reset("x")
    assert rl.check("x")[0] is True


# ── EnforcementMiddleware ─────────────────────────────────────────────────────

def test_allows_when_unconfigured():
    mw = EnforcementMiddleware()
    called = {}
    def handler(r):
        called["yes"] = True
        return "ok"
    assert mw.wrap_tool_call(_req("foo"), handler) == "ok"
    assert called.get("yes")


def test_denies_listed_tool_without_executing():
    mw = EnforcementMiddleware(disallowed_tools=["danger"])
    called = {}
    def handler(r):
        called["ran"] = True
        return "ok"
    out = mw.wrap_tool_call(_req("danger", call_id="abc"), handler)
    assert isinstance(out, ToolMessage)
    assert out.tool_call_id == "abc"
    assert "policy" in out.content.lower()
    assert "ran" not in called  # handler never invoked


def test_predicate_blocks_with_reason():
    def deny_big(name, args):
        if args.get("n", 0) > 10:
            return "n too large"
        return None
    mw = EnforcementMiddleware(predicate=deny_big)
    assert mw.wrap_tool_call(_req("t", {"n": 5}), lambda r: "ok") == "ok"
    out = mw.wrap_tool_call(_req("t", {"n": 99}), lambda r: "ok")
    assert isinstance(out, ToolMessage) and "n too large" in out.content


def test_rate_limit_blocks_third_call():
    mw = EnforcementMiddleware(rate_limits={"t": {"max": 2, "window_seconds": 60}})
    h = lambda r: "ok"
    assert mw.wrap_tool_call(_req("t"), h) == "ok"
    assert mw.wrap_tool_call(_req("t"), h) == "ok"
    out = mw.wrap_tool_call(_req("t"), h)
    assert isinstance(out, ToolMessage) and "Rate limit" in out.content


@pytest.mark.asyncio
async def test_async_path_blocks_and_allows():
    mw = EnforcementMiddleware(disallowed_tools=["bad"])
    async def handler(r):
        return "ran"
    assert await mw.awrap_tool_call(_req("good"), handler) == "ran"
    out = await mw.awrap_tool_call(_req("bad", call_id="z"), handler)
    assert isinstance(out, ToolMessage) and out.tool_call_id == "z"


def test_config_wires_enforcement(monkeypatch, tmp_path):
    """When enabled with a deny list, _build_middleware prepends the gate."""
    import yaml
    from graph.config import LangGraphConfig
    from graph.agent import _build_middleware

    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({
        "middleware": {"enforcement": True, "knowledge": False, "audit": False,
                       "memory": False},
        "enforcement": {"disallowed_tools": ["rm_rf"]},
    }))
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.enforcement_enabled is True
    assert cfg.enforcement_disallowed_tools == ["rm_rf"]
    mw = _build_middleware(cfg, knowledge_store=None)
    names = [m.__class__.__name__ for m in mw]
    assert "EnforcementMiddleware" in names
    # Gate is the outermost *tool* wrapper — it must precede the other
    # tool-call middleware (audit/ingest/message-capture) so blocked calls
    # never execute. (PromptCacheMiddleware wraps the model call, not tools.)
    assert names.index("EnforcementMiddleware") < names.index("MessageCaptureMiddleware")
