"""Quota-aware model failover (ADR 0122 D2/D4/D8, #4437).

Host-free: fake model instances, fake 429s, and a fake usage store — no network, no
gateway, no real usage snapshot. Covers the quota helpers, the quota-aware
``ObservableModelFallbackMiddleware`` (sync + async), and the per-tab
``ModelOverrideMiddleware`` same-account-quota guard.
"""

from __future__ import annotations

import pytest
from langgraph.errors import GraphBubbleUp

from graph.agent import ObservableModelFallbackMiddleware
from graph.config import LangGraphConfig
from graph.middleware import model_override as mo
from graph.middleware.quota_routing import (
    account_for_model,
    is_blocked_for_model,
    quota_429,
    resolve_account,
)


# ── fakes ────────────────────────────────────────────────────────────────────────


class _Model:
    """A built chat-model stand-in — only its name matters to the routing code."""

    def __init__(self, name: str, provider: str | None = None):
        from graph.providers.identity import tag_model_provider

        self.model_name = name
        if provider is None:
            provider = name.split(":", 1)[0] if ":" in name else (
                "anthropic-oauth" if name.startswith("claude-") else "openai"
            )
        tag_model_provider(self, provider, provider)


class _Req:
    """Minimal ModelRequest with the immutable ``override`` contract."""

    def __init__(self, model, state=None):
        self.model = model
        self.state = state or {}

    def override(self, **kw):
        return _Req(kw.get("model", self.model), dict(self.state))


class _Err(RuntimeError):
    """A fake upstream model-call error carrying an HTTP status (+ optional body/headers),
    shaped like the openai SDK errors the classifiers read."""

    def __init__(self, status: int, *, body=None, headers=None, message: str = ""):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status
        if body is not None:
            self.body = body
        if headers is not None:
            self.response = type("_Resp", (), {"headers": headers})()


class _FakeStore:
    """A UsageStore stand-in: ``cached(provider_id)`` returns a preset snapshot or None.
    Never polls — exactly what the middleware is required to use (no forced poll)."""

    def __init__(self, snapshots=None):
        self._snapshots = snapshots or {}

    def cached(self, provider_id):
        return self._snapshots.get(provider_id)


def _blocked_snapshot(scope=None):
    return {"status": "ok", "stale": False, "windows": [{"status": "blocked", "scope": scope}]}


def _quota_429(message="exceeded your weekly usage limit", headers=None):
    return _Err(429, body={"error": {"message": message}}, headers=headers, message="429")


def _capture_events(monkeypatch):
    """Record every ``HOST.publish`` call so a test can inspect the model.fallback payload."""
    from graph.plugins import host

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(host.HOST, "publish", lambda ev, data: events.append((ev, data)))
    return events


# ── helpers: account resolution ───────────────────────────────────────────────────


def test_resolve_account_qualified_native_and_gateway():
    cfg = LangGraphConfig(model_provider="openai", model_name="protolabs/smart")
    assert resolve_account("anthropic-oauth:claude-opus-4-8", cfg) == ("anthropic-oauth", "claude-opus-4-8")
    assert resolve_account("openai-codex:gpt-5.6", cfg) == ("openai-codex", "gpt-5.6")
    # A gateway alias / bare model under a non-native lead is an unknown account.
    assert account_for_model("protolabs/fast", cfg) is None
    assert account_for_model("protolabs/smart", cfg) is None


def test_resolve_account_unqualified_follows_default_provider():
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-4-8")
    assert account_for_model("claude-opus-4-8", cfg) == "anthropic-oauth"


# ── helpers: quota_429 ─────────────────────────────────────────────────────────────


def test_quota_429_body_marker():
    assert quota_429(_quota_429("you have exceeded your usage limit")) is True


def test_quota_429_native_oauth_any_429():
    assert quota_429(_Err(429, message="server busy"), native_oauth=True) is True


def test_quota_429_plain_gateway_throttle_is_not_quota():
    # A bare 429 with no usage/rate-limit marker from a non-native provider is NOT quota.
    assert quota_429(_Err(429, message="please slow down"), native_oauth=False) is False


def test_quota_429_non_429_is_false():
    assert quota_429(_Err(500, message="boom"), native_oauth=True) is False
    assert quota_429(None) is False


# ── helpers: is_blocked_for_model ───────────────────────────────────────────────────


def test_is_blocked_unscoped_and_scoped():
    store = _FakeStore({"anthropic-oauth": _blocked_snapshot()})
    assert is_blocked_for_model(store, "anthropic-oauth", "claude-opus-4-8") is True

    scoped = _FakeStore({"anthropic-oauth": _blocked_snapshot(scope={"model": "claude-opus-4-8"})})
    assert is_blocked_for_model(scoped, "anthropic-oauth", "claude-opus-4-8") is True
    # D2: a window scoped to one model never blocks another.
    assert is_blocked_for_model(scoped, "anthropic-oauth", "claude-sonnet-5") is False


def test_is_blocked_unknown_snapshot_is_false():
    assert is_blocked_for_model(_FakeStore({}), "anthropic-oauth", "x") is False  # absent
    stale = _FakeStore({"anthropic-oauth": {"status": "ok", "stale": True, "windows": [{"status": "blocked"}]}})
    assert is_blocked_for_model(stale, "anthropic-oauth", "x") is False  # stale → unknown
    assert is_blocked_for_model(_FakeStore({}), None, "x") is False  # no account


# ── ObservableModelFallbackMiddleware: quota-aware failover ─────────────────────────


def _fallback_mw(fallback_names, *, model_provider, model_name, store=None):
    cfg = LangGraphConfig(
        model_provider=model_provider,
        model_name=model_name,
        routing_fallback_models=list(fallback_names),
    )
    models = [_Model(n.split(":", 1)[-1]) for n in fallback_names]
    return ObservableModelFallbackMiddleware(*models, config=cfg, usage_store=store or _FakeStore())


def test_same_account_quota_reraises_primary_no_fallback():
    """r1: the only fallback shares the primary's account — on a quota 429 it is skipped and
    the PRIMARY exception is re-raised, no fallback attempted."""
    mw = _fallback_mw(
        ["anthropic-oauth:claude-sonnet-5"], model_provider="anthropic-oauth", model_name="claude-opus-4-8",
        store=_FakeStore({"anthropic-oauth": _blocked_snapshot()}),
    )
    primary_exc = _quota_429()
    calls: list[str] = []

    def handler(req):
        calls.append(req.model.model_name)
        raise primary_exc

    with pytest.raises(RuntimeError) as ei:
        mw.wrap_model_call(_Req(_Model("claude-opus-4-8")), handler)
    assert ei.value is primary_exc
    assert calls == ["claude-opus-4-8"]  # only the primary ran — same-account fallback skipped


def test_cross_account_quota_fails_over_and_emits_event(monkeypatch):
    """r2: a fallback on a DIFFERENT account serves the quota-429 turn and a model.fallback
    event is emitted."""
    events = _capture_events(monkeypatch)
    mw = _fallback_mw(["openai-codex:gpt-5.6"], model_provider="anthropic-oauth", model_name="claude-opus-4-8")
    primary_exc = _quota_429()

    def handler(req):
        if req.model.model_name == "claude-opus-4-8":
            raise primary_exc
        return "served by codex"

    assert mw.wrap_model_call(_Req(_Model("claude-opus-4-8")), handler) == "served by codex"
    assert len(events) == 1 and events[0][0] == "model.fallback"
    assert events[0][1]["fallback_model"] == "gpt-5.6"
    assert events[0][1]["skipped_blocked"] == []


def test_blocked_fallback_skipped_next_tried(monkeypatch):
    """r3: a fallback whose account has a blocked window applying to its model is skipped
    (logged, named in skipped_blocked), and the next eligible fallback serves the turn."""
    events = _capture_events(monkeypatch)
    store = _FakeStore({"anthropic-oauth": _blocked_snapshot()})  # first fallback's account is blocked
    mw = _fallback_mw(
        ["anthropic-oauth:claude-opus-4-8", "openai-codex:gpt-5.6"],
        model_provider="openai",
        model_name="protolabs/smart",
        store=store,
    )
    primary_exc = _Err(500, message="primary boom")  # non-quota primary — the blocked-skip still applies

    def handler(req):
        name = req.model.model_name
        if name == "protolabs/smart":
            raise primary_exc
        if name == "claude-opus-4-8":
            raise AssertionError("a blocked fallback must never be attempted")
        return "served by codex"

    assert mw.wrap_model_call(_Req(_Model("protolabs/smart")), handler) == "served by codex"
    assert events[0][1]["fallback_model"] == "gpt-5.6"
    assert events[0][1]["fallback_index"] == 1
    assert events[0][1]["skipped_blocked"] == ["claude-opus-4-8"]


def test_unknown_usage_preserves_today_order(monkeypatch):
    """r4: a gateway primary (unknown account) + gateway fallbacks with no snapshot fail over
    in exactly today's order — every fallback tried in turn, nothing skipped."""
    events = _capture_events(monkeypatch)
    mw = _fallback_mw(
        ["protolabs/a", "protolabs/b"], model_provider="openai", model_name="protolabs/smart"
    )
    primary_exc = _quota_429()  # quota body, but a gateway primary has no resolvable account
    order: list[str] = []

    def handler(req):
        name = req.model.model_name
        order.append(name)
        if name == "protolabs/smart":
            raise primary_exc
        if name == "protolabs/a":
            raise _Err(500, message="a down")
        return "b served"

    assert mw.wrap_model_call(_Req(_Model("protolabs/smart")), handler) == "b served"
    assert order == ["protolabs/smart", "protolabs/a", "protolabs/b"]  # today's order, nothing skipped
    assert events[0][1]["skipped_blocked"] == []


def test_no_config_behaves_exactly_as_today():
    """r4: constructed the old way (no config/store) — pure today's behaviour, and no usage
    store is ever resolved (would touch disk)."""
    mw = ObservableModelFallbackMiddleware(_Model("a"), _Model("b"))
    primary_exc = _Err(429, message="slow down")
    order: list[str] = []

    def handler(req):
        name = req.model.model_name
        order.append(name)
        if name == "primary":
            raise primary_exc
        if name == "a":
            raise RuntimeError("a down")
        return "ok"

    assert mw.wrap_model_call(_Req(_Model("primary")), handler) == "ok"
    assert order == ["primary", "a", "b"]


def test_graphbubbleup_from_primary_is_reraised_untouched():
    """r6: a HITL interrupt out of the primary call is control flow — re-raised immediately,
    no fallback attempted."""
    mw = _fallback_mw(["openai-codex:gpt-5.6"], model_provider="anthropic-oauth", model_name="claude-opus-4-8")
    calls: list[str] = []

    def handler(req):
        calls.append(req.model.model_name)
        raise GraphBubbleUp()

    with pytest.raises(GraphBubbleUp):
        mw.wrap_model_call(_Req(_Model("claude-opus-4-8")), handler)
    assert calls == ["claude-opus-4-8"]  # never reached a fallback


@pytest.mark.asyncio
async def test_async_same_account_quota_reraises_primary():
    """r6: the async path enforces the same cross-account-only rule on a quota 429."""
    mw = _fallback_mw(
        ["anthropic-oauth:claude-sonnet-5"], model_provider="anthropic-oauth", model_name="claude-opus-4-8",
        store=_FakeStore({"anthropic-oauth": _blocked_snapshot()}),
    )
    primary_exc = _quota_429()
    calls: list[str] = []

    async def handler(req):
        calls.append(req.model.model_name)
        raise primary_exc

    with pytest.raises(RuntimeError) as ei:
        await mw.awrap_model_call(_Req(_Model("claude-opus-4-8")), handler)
    assert ei.value is primary_exc
    assert calls == ["claude-opus-4-8"]


@pytest.mark.asyncio
async def test_async_cross_account_quota_fails_over(monkeypatch):
    """r6: the async path fails over across accounts and skips a blocked one."""
    events = _capture_events(monkeypatch)
    store = _FakeStore({"anthropic-oauth": _blocked_snapshot()})
    mw = _fallback_mw(
        ["anthropic-oauth:claude-opus-4-8", "openai-codex:gpt-5.6"],
        model_provider="anthropic-oauth",
        model_name="claude-opus-4-8",
        store=store,
    )
    primary_exc = _quota_429()

    async def handler(req):
        name = req.model.model_name
        if name == "claude-opus-4-8":
            raise primary_exc  # primary (and the same-account, also-blocked fallback) ruled out
        return "served by codex"

    assert await mw.awrap_model_call(_Req(_Model("claude-opus-4-8")), handler) == "served by codex"
    assert events[0][1]["fallback_model"] == "gpt-5.6"
    assert events[0][1]["skipped_blocked"] == ["claude-opus-4-8"]


# ── ModelOverrideMiddleware: same-account quota guard ───────────────────────────────


def _patch_create_llm(monkeypatch):
    def _fake(config, *, model_name=None, reasoning_effort=None):
        return _Model(model_name)

    monkeypatch.setattr("graph.llm.create_llm", _fake)


def test_override_does_not_degrade_to_same_account_default_on_quota(monkeypatch):
    """r5: a per-tab pick that hits its subscription quota is NOT degraded to a default on
    the same account — a QuotaExceededError is surfaced, naming the reset time."""
    _patch_create_llm(monkeypatch)
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-4-8")
    mw = mo.ModelOverrideMiddleware(cfg)
    req = _Req(_Model("claude-opus-4-8"), state={"model": "anthropic-oauth:claude-sonnet-5"})
    quota = _quota_429(headers={"retry-after": "120"})
    calls: list[str] = []

    def handler(r):
        calls.append(r.model.model_name)
        raise quota

    with pytest.raises(mo.QuotaExceededError) as ei:
        mw.wrap_model_call(req, handler)
    assert ei.value.model == "anthropic-oauth:claude-sonnet-5"
    assert ei.value.resets_at  # reset time surfaced from the 429's Retry-After
    assert calls == ["anthropic-oauth:claude-sonnet-5"]  # the default was NOT tried


def test_override_cross_account_quota_is_not_special_cased(monkeypatch):
    """A pick on a DIFFERENT account than the default keeps today's handling (the raw error
    propagates — the default could still serve); no QuotaExceededError."""
    _patch_create_llm(monkeypatch)
    cfg = LangGraphConfig(model_provider="openai", model_name="protolabs/smart")  # default = gateway (unknown acct)
    mw = mo.ModelOverrideMiddleware(cfg)
    req = _Req(_Model("protolabs/smart"), state={"model": "anthropic-oauth:claude-sonnet-5"})
    quota = _quota_429()

    def handler(r):
        raise quota

    with pytest.raises(_Err) as ei:
        mw.wrap_model_call(req, handler)
    assert ei.value is quota
    assert not isinstance(ei.value, mo.QuotaExceededError)


def test_override_same_account_non_quota_is_not_special_cased(monkeypatch):
    """The guard is quota-specific: a same-account 5xx is not a QuotaExceededError."""
    _patch_create_llm(monkeypatch)
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-4-8")
    mw = mo.ModelOverrideMiddleware(cfg)
    req = _Req(_Model("claude-opus-4-8"), state={"model": "anthropic-oauth:claude-sonnet-5"})
    boom = _Err(500, message="transient 5xx")

    def handler(r):
        raise boom

    with pytest.raises(_Err) as ei:
        mw.wrap_model_call(req, handler)
    assert ei.value is boom


@pytest.mark.asyncio
async def test_async_override_does_not_degrade_same_account_quota(monkeypatch):
    """r5/r6: the async override path enforces the same same-account-quota guard."""
    _patch_create_llm(monkeypatch)
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-4-8")
    mw = mo.ModelOverrideMiddleware(cfg)
    req = _Req(_Model("claude-opus-4-8"), state={"model": "anthropic-oauth:claude-sonnet-5"})

    async def handler(r):
        raise _quota_429()

    with pytest.raises(mo.QuotaExceededError):
        await mw.awrap_model_call(req, handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("snapshot", [
    None,
    {"status": "ok", "stale": True, "windows": [{"status": "blocked"}]},
    {"status": "unknown", "windows": [{"status": "blocked"}]},
    {"status": "ok", "windows": []},
    _blocked_snapshot({"model": "claude-opus-4-8"}),
])
@pytest.mark.parametrize("error", [_Err(429, message="slow down"), _quota_429()])
async def test_same_account_retry_requires_applicable_block(snapshot, error, async_call):
    mw = _fallback_mw(
        ["anthropic-oauth:claude-sonnet-5"], model_provider="anthropic-oauth",
        model_name="claude-opus-4-8", store=_FakeStore({"anthropic-oauth": snapshot}),
    )
    calls = []

    def handler(req):
        calls.append(req.model.model_name)
        if len(calls) == 1:
            raise error
        return "sonnet served"

    async def async_handler(req):
        return handler(req)

    req = _Req(_Model("claude-opus-4-8"))
    result = await mw.awrap_model_call(req, async_handler) if async_call else mw.wrap_model_call(req, handler)
    assert result == "sonnet served"
    assert calls == ["claude-opus-4-8", "claude-sonnet-5"]


@pytest.mark.parametrize("state", [{}, {"model": "anthropic-oauth:claude-opus-4-8"}])
def test_actual_model_identity_overrides_lead_and_stale_pick(state):
    from types import SimpleNamespace

    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-4-8")
    # Both a subagent without state.model and an inherited pick already degraded
    # to default must follow the actual client, including a bound client.
    actual = _Model("gpt-5.6", provider="openai-codex")
    assert resolve_account(actual, cfg) == ("openai-codex", "gpt-5.6")
    assert resolve_account(SimpleNamespace(bound=actual), cfg) == ("openai-codex", "gpt-5.6")
    assert account_for_model(_Model("claude-opus-4-8", provider="openai"), cfg) is None
    assert account_for_model(SimpleNamespace(model_name="claude-opus-4-8"), cfg) is None

    class RecordingStore(_FakeStore):
        def __init__(self):
            super().__init__()
            self.reads = []

        def cached(self, provider_id):
            self.reads.append(provider_id)
            return super().cached(provider_id)

    store = RecordingStore()
    mw = _fallback_mw(["protolabs/fast"], model_provider="openai",
                      model_name="anthropic-oauth:claude-opus-4-8", store=store)
    mw._fallback_plan(_Req(actual, state), _quota_429())
    assert store.reads == ["openai-codex"]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_call", [False, True])
async def test_quota_pick_preserves_turn_error_retry_metadata(monkeypatch, async_call):
    from server.chat import turn_error
    from datetime import datetime

    _patch_create_llm(monkeypatch)
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-4-8")
    mw = mo.ModelOverrideMiddleware(cfg)
    req = _Req(_Model("claude-opus-4-8"), {"model": "anthropic-oauth:claude-sonnet-5"})
    quota = _quota_429(headers={"retry-after": "86400"})

    def handler(req):
        raise quota

    async def async_handler(req):
        return handler(req)

    with pytest.raises(mo.QuotaExceededError) as caught:
        if async_call:
            await mw.awrap_model_call(req, async_handler)
        else:
            mw.wrap_model_call(req, handler)
    result = turn_error(caught.value)
    assert result["type"] == "rate_limit_error"
    assert result["upstream_status"] == 429
    assert result["retry_after"] == 86400
    assert abs((datetime.fromisoformat(result["retry_at"]) -
                datetime.fromisoformat(caught.value.resets_at)).total_seconds()) < 2
    assert not result.get("model_unavailable")


@pytest.mark.asyncio
async def test_async_graphbubbleup_untouched():
    mw = _fallback_mw(["openai-codex:gpt-5.6"], model_provider="anthropic-oauth",
                      model_name="claude-opus-4-8")
    interrupt = GraphBubbleUp()
    calls = []

    async def handler(req):
        calls.append(req.model.model_name)
        raise interrupt

    with pytest.raises(GraphBubbleUp) as caught:
        await mw.awrap_model_call(_Req(_Model("claude-opus-4-8")), handler)
    assert caught.value is interrupt
    assert calls == ["claude-opus-4-8"]
