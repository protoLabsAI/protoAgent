"""U7 — delegate→account mapping + ``graph.sdk.provider_usage()`` (ADR 0122 D2/D4/D6/D7/D8).

Host-free: a fake/duck-typed usage store and fake fetchers, no network. Covers the delegate
inference (incl. the npx-args form and the explicit ``account:`` override), the None-degrade
contract (unresolvable account / missing / stale / unknown / raising store), the fresh-snapshot
headroom math, the scoped-window rule (a Fable-scoped block must not make Opus unusable), and
the no-forced-poll guarantee (two reads inside 60 s → at most one fetch).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from graph import sdk
from graph.providers.usage import Account, Window
from graph.providers.usage import accounts as acct_mod
from observability import usage_store
from observability.usage_store import UsageStore

RESET = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
RESET_ISO = RESET.astimezone(UTC).isoformat()


@pytest.fixture(autouse=True)
def _reset_default_store():
    yield
    usage_store.set_default_store(None)


# ── r1: account_for_delegate ─────────────────────────────────────────────────────────


def test_claude_agent_acp_infers_anthropic():
    assert acct_mod.account_for_delegate({"command": "claude-agent-acp", "args": []}) == "anthropic-oauth"


def test_claude_code_alias_infers_anthropic():
    # The ACP adapter rewrites `claude-code`/`claude-acp` to claude-agent-acp — the same login.
    assert acct_mod.account_for_delegate({"command": "claude-code"}) == "anthropic-oauth"


def test_codex_acp_npx_args_infers_openai():
    entry = {"command": "npx", "args": ["-y", "@agentclientprotocol/codex-acp"]}
    assert acct_mod.account_for_delegate(entry) == "openai-codex"


def test_explicit_account_overrides_inference():
    entry = {"command": "npx", "args": ["-y", "@agentclientprotocol/codex-acp"], "account": "anthropic-oauth"}
    assert acct_mod.account_for_delegate(entry) == "anthropic-oauth"


def test_explicit_account_without_command():
    assert acct_mod.account_for_delegate({"account": "openai-codex"}) == "openai-codex"


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "gemini", "args": ["--experimental-acp"]},  # a non-mapped ACP agent
        {"command": "", "args": []},
        {"command": 5, "args": [1, 2]},  # non-string command/args
        {"args": "not-a-list"},
        {},
        None,
        123,
        "",
        "  ",
    ],
)
def test_unknown_or_malformed_entry_returns_none_without_raising(entry):
    assert acct_mod.account_for_delegate(entry) is None


def test_name_lookup_resolves_against_configured_roster(monkeypatch):
    roster = [
        {"name": "codey", "command": "npx", "args": ["-y", "@agentclientprotocol/codex-acp"]},
        {"name": "claudey", "command": "claude-agent-acp"},
    ]
    monkeypatch.setattr(acct_mod, "_configured_delegates", lambda: roster)
    assert acct_mod.account_for_delegate("codey") == "openai-codex"
    assert acct_mod.account_for_delegate("claudey") == "anthropic-oauth"
    assert acct_mod.account_for_delegate("nobody") is None


# ── helpers for the store-backed provider_usage tests ────────────────────────────────


def _window(wid: str, used: float, *, status: str = "ok", scope: dict | None = None) -> Window:
    return Window(id=wid, kind="session", window_seconds=18000, used_pct=used, resets_at=RESET, status=status, scope=scope)


def _snapshot(provider_id: str, windows: list[Window], *, status: str = "ok", stale: bool = False) -> dict:
    """An ``Account.to_dict()`` + ``stale`` flag, exactly as ``UsageStore.get`` returns it."""
    acct = Account(provider_id, provider_id, account_key="acct-1")
    acct.status = status
    acct.windows = windows
    data = acct.to_dict()
    data["stale"] = stale
    if stale:  # the store presents a stale snapshot as unknown (keeps the greyed-out windows)
        data["status"] = "unknown"
    return data


class FakeStore:
    """Duck-typed stand-in for ``UsageStore`` — a canned ``get()`` per provider and a call log."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[tuple[str, bool]] = []

    def get(self, provider_id: str, *, force: bool = False) -> dict:
        self.calls.append((provider_id, force))
        resp = self._responses.get(provider_id)
        if callable(resp):
            return resp()
        if resp is None:  # nothing cached → the store's "not connected" unknown
            return _snapshot(provider_id, [], status="unknown")
        return resp


def _install(responses: dict) -> FakeStore:
    store = FakeStore(responses)
    usage_store.set_default_store(store)
    return store


# ── r2: None-degrade contract ─────────────────────────────────────────────────────────


def test_unresolvable_account_is_none():
    _install({})
    assert sdk.provider_usage() is None
    assert sdk.provider_usage(delegate={"command": "gemini"}) is None


def test_missing_entry_is_none():
    _install({})  # unknown provider → "not connected" unknown snapshot
    assert sdk.provider_usage("anthropic-oauth") is None


def test_stale_snapshot_is_none():
    _install({"anthropic-oauth": _snapshot("anthropic-oauth", [_window("a:0", 10.0)], stale=True)})
    assert sdk.provider_usage("anthropic-oauth") is None


def test_unknown_status_is_none():
    _install({"anthropic-oauth": _snapshot("anthropic-oauth", [], status="unknown")})
    assert sdk.provider_usage("anthropic-oauth") is None


def test_raising_store_is_none():
    def boom():
        raise RuntimeError("store exploded")

    _install({"anthropic-oauth": boom})
    assert sdk.provider_usage("anthropic-oauth") is None


@pytest.mark.parametrize("value", [None, "", "invalid", True, float("nan"), float("inf")])
def test_invalid_usage_percentage_degrades_entire_snapshot(value):
    data = _snapshot("anthropic-oauth", [_window("valid", 42.5), _window("invalid", 10.0)])
    data["windows"][1]["used_pct"] = value
    _install({"anthropic-oauth": data})
    assert sdk.provider_usage("anthropic-oauth") is None


def test_missing_usage_percentage_degrades_entire_snapshot():
    data = _snapshot("anthropic-oauth", [_window("valid", 42.5), _window("missing", 10.0)])
    del data["windows"][1]["used_pct"]
    _install({"anthropic-oauth": data})
    assert sdk.provider_usage("anthropic-oauth") is None


def test_zero_usage_percentage_is_valid():
    _install({"anthropic-oauth": _snapshot("anthropic-oauth", [_window("unused", 0.0)])})
    assert sdk.provider_usage("anthropic-oauth")["headroom_pct"] == 100.0


# ── r3: fresh snapshot → headroom math ────────────────────────────────────────────────


def test_fresh_snapshot_reports_headroom_and_binding_window():
    windows = [_window("sess", 10.0), _window("weekly", 42.5)]
    _install({"anthropic-oauth": _snapshot("anthropic-oauth", windows)})
    out = sdk.provider_usage("anthropic-oauth")
    assert out == {
        "usable": True,
        "headroom_pct": 57.5,  # 100 − max(10, 42.5)
        "binding_window": "weekly",
        "resets_at": RESET_ISO,
    }


def test_resolves_account_from_delegate():
    windows = [_window("sess", 20.0)]
    _install({"anthropic-oauth": _snapshot("anthropic-oauth", windows)})
    out = sdk.provider_usage(delegate={"command": "claude-agent-acp"})
    assert out is not None and out["binding_window"] == "sess" and out["headroom_pct"] == 80.0


@pytest.mark.parametrize("multiple_blocks", [False, True])
def test_blocked_binding_uses_latest_reset_not_highest_usage(multiple_blocks):
    blocked = _window("late-block", 10.0, status="blocked")
    blocked.resets_at = RESET + timedelta(hours=2)
    windows = [_window("higher-usage", 90.0), blocked]
    if multiple_blocks:
        windows.append(_window("early-block", 100.0, status="blocked"))
    _install({"anthropic-oauth": _snapshot("anthropic-oauth", windows)})
    assert sdk.provider_usage("anthropic-oauth") == {
        "usable": False,
        "headroom_pct": 0.0,
        "binding_window": "late-block",
        "resets_at": blocked.resets_at.isoformat(),
    }


# ── r4: a scoped block binds only its own model (the 08-11 incident) ─────────────────


def test_scoped_block_only_affects_its_model():
    windows = [
        _window("unscoped", 20.0),
        _window("fable-block", 100.0, status="blocked", scope={"model": "fable"}),
    ]
    _install({"anthropic-oauth": _snapshot("anthropic-oauth", windows)})

    fable = sdk.provider_usage("anthropic-oauth", model="fable")
    assert fable == {
        "usable": False,
        "headroom_pct": 0.0,  # the fable-scoped window applies and tops out
        "binding_window": "fable-block",
        "resets_at": RESET_ISO,
    }

    opus = sdk.provider_usage("anthropic-oauth", model="opus")
    assert opus["usable"] is True  # the fable block is ignored for opus
    assert opus["binding_window"] == "unscoped"
    assert opus["headroom_pct"] == 80.0


@pytest.mark.parametrize("scope", [{"model": "fable"}, {"bucket": "codex"}, {"model": "fable", "bucket": "codex"}])
def test_model_none_counts_only_unscoped_windows(scope):
    windows = [
        _window("unscoped", 30.0),
        _window("scoped-block", 100.0, status="blocked", scope=scope),
    ]
    _install({"anthropic-oauth": _snapshot("anthropic-oauth", windows)})
    out = sdk.provider_usage("anthropic-oauth")  # model=None
    assert out["usable"] is True  # the scoped block is not an unscoped window
    assert out["binding_window"] == "unscoped"
    assert out["headroom_pct"] == 70.0


@pytest.mark.parametrize("model", [None, "codex"])
def test_bucket_only_block_has_no_applicable_window(model):
    windows = [_window("bucket-block", 100.0, status="blocked", scope={"bucket": "codex"})]
    _install({"openai-codex": _snapshot("openai-codex", windows)})
    assert sdk.provider_usage("openai-codex", model=model) == {
        "usable": True,
        "headroom_pct": 100.0,
        "binding_window": None,
        "resets_at": None,
    }


# ── r5: never forces a poll ───────────────────────────────────────────────────────────


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_two_calls_within_60s_cause_at_most_one_fetch():
    clock = _FakeClock()
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return _snapshot_account("anthropic-oauth")

    store = UsageStore({"anthropic-oauth": fetcher}, clock=clock)
    usage_store.set_default_store(store)

    assert sdk.provider_usage("anthropic-oauth") is not None
    sdk.provider_usage("anthropic-oauth")  # still inside the 60 s poll gate
    assert calls["n"] == 1

    clock.advance(61)
    sdk.provider_usage("anthropic-oauth")  # gate elapsed → one more fetch, still no force
    assert calls["n"] == 2


def _snapshot_account(provider_id: str) -> Account:
    acct = Account(provider_id, provider_id, account_key="acct-1")
    acct.status = "ok"
    acct.windows = [_window(f"{provider_id}:0", 10.0)]
    return acct
