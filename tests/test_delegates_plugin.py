"""Tests for the unified delegate registry plugin (ADR 0025, PR1).

Covers adapter parse/validation per type, secret resolution, the registry
(parse + drop bad + dispatch routing), the delegate_to tool, and a2a/openai/acp
dispatch with fakes.
"""

from __future__ import annotations

import pytest

import plugins.delegates as P
from plugins.delegates.adapters import (
    ADAPTERS,
    DelegateError,
    _secret,
    delegate_types,
)
from plugins.delegates.registry import DelegateRegistry


# ── adapter parse / validation ────────────────────────────────────────────────


def test_a2a_parse_ok_and_missing_url():
    d = ADAPTERS["a2a"].parse(
        {"name": "helm", "type": "a2a", "url": "https://h/a2a", "auth": {"scheme": "bearer", "token": "sek"}}
    )
    assert d.name == "helm" and d.url == "https://h/a2a"
    assert d.auth_scheme == "bearer" and d.auth_token == "sek"
    with pytest.raises(DelegateError):
        ADAPTERS["a2a"].parse({"name": "x", "type": "a2a"})  # no url


def test_openai_parse_ok_and_requires_url_model():
    d = ADAPTERS["openai"].parse(
        {
            "name": "opus",
            "type": "openai",
            "url": "https://g/v1",
            "model": "protolabs/reasoning",
            "api_key": "k",
            "max_tokens": "50",
            "temperature": "0.1",
        }
    )
    assert d.model == "protolabs/reasoning" and d.api_key == "k"
    assert d.max_tokens == 50 and d.temperature == pytest.approx(0.1)
    with pytest.raises(DelegateError):
        ADAPTERS["openai"].parse({"name": "x", "type": "openai", "url": "https://g/v1"})  # no model


def test_acp_parse_ok_and_requires_command_workdir():
    d = ADAPTERS["acp"].parse(
        {
            "name": "proto",
            "type": "acp",
            "command": "proto",
            "args": ["--acp"],
            "workdir": "/tmp",
            "permissions": "READONLY",
            "confirm": "true",
        }
    )
    assert d.command == "proto" and d.args == ["--acp"] and d.workdir == "/tmp"
    assert d.permissions == "readonly" and d.confirm is True
    with pytest.raises(DelegateError):
        ADAPTERS["acp"].parse({"name": "x", "type": "acp", "command": "proto"})  # no workdir


def test_acp_parse_claude_code_alias():
    # `claude-code` is a convenience alias for the claude-agent-acp adapter (#1116):
    # the operator's intuitive name maps to the real binary, with no launch args.
    d = ADAPTERS["acp"].parse(
        {"name": "cc", "type": "acp", "command": "claude-code", "args": ["--stray"], "workdir": "/tmp"}
    )
    assert d.command == "claude-agent-acp" and d.args == []


async def test_acp_probe_bare_claude_hints_the_adapter():
    # `claude` is on PATH but has no native ACP mode — the probe must steer to the
    # adapter rather than show green (the false-green the old PATH check gave, #1116).
    d = ADAPTERS["acp"].parse({"name": "x", "type": "acp", "command": "claude", "workdir": "/tmp"})
    res = await ADAPTERS["acp"].probe(d)
    assert res["ok"] is False and "claude-agent-acp" in res["error"]


async def test_acp_probe_fails_when_handshake_fails(monkeypatch):
    # A command on PATH + valid workdir that does NOT speak ACP must FAIL the probe —
    # the core fix for #1116 (PATH+workdir alone gave false confidence).
    import sys

    from plugins.coding_agent.acp_client import AcpClient, AcpError

    async def _boom(self):
        raise AcpError("agent exited")

    async def _noop(self):
        pass

    monkeypatch.setattr(AcpClient, "handshake", _boom)
    monkeypatch.setattr(AcpClient, "close", _noop)
    d = ADAPTERS["acp"].parse({"name": "x", "type": "acp", "command": sys.executable, "workdir": "/tmp"})
    res = await ADAPTERS["acp"].probe(d)
    assert res["ok"] is False and "handshake failed" in res["error"]


async def test_acp_probe_ok_on_successful_handshake(monkeypatch):
    import sys

    from plugins.coding_agent.acp_client import AcpClient

    async def _ok(self):
        self._protocol_version = 1

    async def _noop(self):
        pass

    monkeypatch.setattr(AcpClient, "handshake", _ok)
    monkeypatch.setattr(AcpClient, "close", _noop)
    d = ADAPTERS["acp"].parse({"name": "x", "type": "acp", "command": sys.executable, "workdir": "/tmp"})
    res = await ADAPTERS["acp"].probe(d)
    assert res["ok"] is True and "handshake OK" in res["detail"]


async def test_acp_probe_resolves_command_against_delegate_env_path(monkeypatch):
    # The probe must resolve the command against the SAME PATH the real spawn uses —
    # the delegate's env PATH overlaid on the process PATH — so a command reachable
    # only via the delegate env doesn't red-X the Test button while the spawn would
    # actually find it (#1299 probe-vs-spawn disagreement).
    import shutil

    seen: dict = {}

    def fake_which(cmd, path=None):
        seen["path"] = path
        return None  # force the not-on-PATH branch (so we never spawn a real process)

    monkeypatch.setattr(shutil, "which", fake_which)
    d = ADAPTERS["acp"].parse(
        {"name": "x", "type": "acp", "command": "npx", "workdir": "/tmp", "env": {"PATH": "/custom/bin"}}
    )
    res = await ADAPTERS["acp"].probe(d)
    assert res["ok"] is False and "not on PATH" in res["error"]
    assert seen["path"] == "/custom/bin"  # resolved against the delegate's env PATH


def test_secret_value_wins_then_env(monkeypatch):
    assert _secret({"token": "explicit"}, "token", "credentialsEnv") == "explicit"
    monkeypatch.setenv("MY_TOK", "fromenv")
    assert _secret({"credentialsEnv": "MY_TOK"}, "token", "credentialsEnv") == "fromenv"
    assert _secret({}, "token", "credentialsEnv") == ""


def test_delegate_types_schema_shape():
    types = {t["type"]: t for t in delegate_types()}
    assert set(types) == {"a2a", "openai", "acp"}
    # each type advertises a field schema with required keys
    for t in types.values():
        assert t["label"] and isinstance(t["fields"], list) and t["fields"]
        for f in t["fields"]:
            assert {"key", "label", "kind"} <= set(f)


# ── registry ──────────────────────────────────────────────────────────────────


def test_registry_parses_and_drops_bad():
    reg = DelegateRegistry(
        [
            {"name": "helm", "type": "a2a", "url": "https://h/a2a"},
            {"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"},
            {"name": "bad", "type": "nope"},  # unknown type
            {"name": "helm", "type": "a2a", "url": "https://dup/a2a"},  # duplicate
            {"name": "incomplete", "type": "acp", "command": "proto"},  # no workdir
            "not-a-dict",
        ]
    )
    assert reg.names() == ["helm", "opus"]
    assert reg.get("helm").url == "https://h/a2a"  # first dup wins
    assert "helm" in reg.listing() and "a2a" in reg.listing()


async def test_registry_dispatch_unknown_raises():
    reg = DelegateRegistry([])
    with pytest.raises(DelegateError):
        await reg.dispatch("nope", "hi")


# ── delegate_to tool ──────────────────────────────────────────────────────────


def _register(delegates, monkeypatch):
    monkeypatch.setattr(P, "_load_delegates_config", lambda: delegates)

    class _Reg:
        def __init__(self):
            self.config = {}
            self.tools = []

        def register_tool(self, t):
            self.tools.append(t)

    r = _Reg()
    P.register(r)
    return r


def test_register_no_delegates_registers_nothing(monkeypatch):
    r = _register([], monkeypatch)
    assert r.tools == []


def test_register_exposes_delegate_to_and_list_agents(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}], monkeypatch)
    assert [t.name for t in r.tools] == ["delegate_to", "list_agents"]
    assert "opus" in r.tools[0].description


def test_registry_roster_shape():
    reg = DelegateRegistry([{"name": "opus", "type": "openai", "url": "https://g/v1",
                             "model": "m", "description": "a model"}])
    assert reg.roster() == [{"name": "opus", "type": "openai", "description": "a model", "url": "https://g/v1"}]


def test_list_agents_lists_roster_with_health(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1",
                    "model": "m", "description": "a model"}], monkeypatch)
    la = next(t for t in r.tools if t.name == "list_agents")
    monkeypatch.setattr("plugins.delegates.health.health_snapshot", lambda: {"opus": {"ok": True}})
    assert "🟢 opus (openai) — a model" in la.invoke({})


def test_list_agents_unknown_health_is_neutral(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}], monkeypatch)
    la = next(t for t in r.tools if t.name == "list_agents")
    monkeypatch.setattr("plugins.delegates.health.health_snapshot", lambda: {})
    assert "⚪ opus (openai)" in la.invoke({})


async def test_delegate_to_unknown_and_empty(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}], monkeypatch)
    tool = r.tools[0]
    assert "unknown delegate" in await tool.ainvoke({"target": "nope", "query": "hi"})
    assert "empty" in (await tool.ainvoke({"target": "opus", "query": "  "})).lower()


# ── dispatch with fakes ───────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload, **kw):
        self._p = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        return _FakeResp(self._p)


async def test_openai_dispatch(monkeypatch):
    import httpx

    payload = {"choices": [{"message": {"content": "the answer"}}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(payload))
    d = ADAPTERS["openai"].parse({"name": "o", "type": "openai", "url": "https://g/v1", "model": "m"})
    assert await ADAPTERS["openai"].dispatch(d, "q") == "the answer"


async def test_a2a_dispatch_inline_reply(monkeypatch):
    import httpx

    # message/send returns an artifact with text → _extract_text picks it up.
    payload = {"result": {"artifacts": [{"parts": [{"kind": "text", "text": "hi from peer"}]}]}}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(payload))
    from security import policy

    monkeypatch.setattr(policy, "check_url", lambda url: None)
    d = ADAPTERS["a2a"].parse({"name": "p", "type": "a2a", "url": "https://p/a2a"})
    assert await ADAPTERS["a2a"].dispatch(d, "q") == "hi from peer"


async def test_a2a_dispatch_sends_version_header(monkeypatch):
    """ADR 0051 Slice 3 — the delegate A2A client MUST send A2A-Version: 1.0, else a
    strict 1.0 peer rejects the call with -32009."""
    import httpx

    captured: dict = {}

    class _CapClient(_FakeClient):
        async def post(self, url, **kw):
            captured["headers"] = kw.get("headers") or {}
            return _FakeResp({"result": {"artifacts": [{"parts": [{"kind": "text", "text": "ok"}]}]}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _CapClient(None))
    from security import policy

    monkeypatch.setattr(policy, "check_url", lambda url: None)
    d = ADAPTERS["a2a"].parse({"name": "p", "type": "a2a", "url": "https://p/a2a"})
    await ADAPTERS["a2a"].dispatch(d, "q")
    assert captured["headers"].get("A2A-Version") == "1.0"


# ── a2a protocol-version negotiation ──────────────────────────────────────────


class _CardClient(_FakeClient):
    """Fake httpx client answering BOTH the agent-card GET (probe / version
    pre-check) and the JSON-RPC POST (dispatch), so the a2a version pre-flight is
    exercised fully offline."""

    def __init__(self, card, rpc=None, **kw):
        self._card = card
        self._rpc = rpc or {"result": {"artifacts": [{"parts": [{"kind": "text", "text": "hi from peer"}]}]}}

    async def get(self, url, **kw):
        return _FakeResp(self._card)

    async def post(self, url, **kw):
        return _FakeResp(self._rpc)


async def test_a2a_probe_returns_peer_protocol_version(monkeypatch):
    """probe() captures the peer's advertised A2A protocol version (the native
    supportedInterfaces field) — distinct from the peer's app `version`."""
    import httpx

    from security import policy

    card = {
        "name": "peer",
        "version": "1.2.3",
        "supportedInterfaces": [{"protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _CardClient(card))
    monkeypatch.setattr(policy, "check_url", lambda url: None)
    d = ADAPTERS["a2a"].parse({"name": "p", "type": "a2a", "url": "https://p/a2a"})
    res = await ADAPTERS["a2a"].probe(d)
    assert res["ok"] is True
    assert res["protocol_version"] == "1.0"
    assert res["supported_versions"] == ["1.0"]
    assert res["version"] == "1.2.3"  # peer APP version, distinct from the protocol version


async def test_a2a_probe_reads_proto_free_hint(monkeypatch):
    """probe() also understands the top-level protocolVersion/supportedVersions
    hint a peer may expose without the proto supportedInterfaces list."""
    import httpx

    from security import policy

    card = {"name": "peer", "protocolVersion": "1.0", "supportedVersions": ["1.0"]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _CardClient(card))
    monkeypatch.setattr(policy, "check_url", lambda url: None)
    d = ADAPTERS["a2a"].parse({"name": "p", "type": "a2a", "url": "https://p/a2a"})
    res = await ADAPTERS["a2a"].probe(d)
    assert res["protocol_version"] == "1.0" and res["supported_versions"] == ["1.0"]


async def test_a2a_dispatch_rejects_version_mismatch(monkeypatch):
    """A peer that clearly advertises an A2A version we can't speak (e.g. 0.3) must
    fail fast with a legible mismatch — not get a 1.0 call sent and wait for an
    opaque -32009 mid-dispatch."""
    import httpx

    from security import policy

    card = {"name": "old-peer", "supportedInterfaces": [{"protocolVersion": "0.3"}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _CardClient(card))
    monkeypatch.setattr(policy, "check_url", lambda url: None)
    d = ADAPTERS["a2a"].parse({"name": "p", "type": "a2a", "url": "https://p/a2a"})
    with pytest.raises(DelegateError) as ei:
        await ADAPTERS["a2a"].dispatch(d, "q")
    msg = str(ei.value)
    assert "0.3" in msg and "refusing" in msg.lower()


async def test_a2a_dispatch_proceeds_when_card_omits_version(monkeypatch):
    """An older peer / partial card that advertises no protocol version must NOT be
    blocked by the best-effort pre-check — dispatch falls through (the -32009
    mapping still covers a genuine incompatibility)."""
    import httpx

    from security import policy

    card = {"name": "peer"}  # nothing about protocol version anywhere
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _CardClient(card))
    monkeypatch.setattr(policy, "check_url", lambda url: None)
    d = ADAPTERS["a2a"].parse({"name": "p", "type": "a2a", "url": "https://p/a2a"})
    assert await ADAPTERS["a2a"].dispatch(d, "q") == "hi from peer"


async def test_acp_dispatch_reuses_client(monkeypatch):
    import plugins.coding_agent as CA

    class _StubClient:
        _permission = None

        async def prompt(self, query, timeout=600.0):
            return "coding done"

    monkeypatch.setattr(CA, "_client_for", lambda spec: _StubClient())
    d = ADAPTERS["acp"].parse({"name": "proto", "type": "acp", "command": "proto", "workdir": "/tmp"})
    assert await ADAPTERS["acp"].dispatch(d, "fix the bug") == "coding done"


async def test_acp_teardown_evicts_the_workdir_scoped_client():
    """teardown reaps the exact cached client dispatch created — proving the
    spec/cache-key (incl. workdir) line up, so a per-call scoped workdir tears
    down its own subprocess."""
    import plugins.coding_agent as CA

    d = ADAPTERS["acp"].parse({"name": "proto", "type": "acp", "command": "proto", "workdir": "/tmp/wt-x"})
    spec = ADAPTERS["acp"]._spec(d)

    class _FakeClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    fake = _FakeClient()
    CA._CLIENTS[CA._cache_key(spec)] = fake

    assert await ADAPTERS["acp"].teardown(d) is True
    assert fake.closed is True
    assert CA._cache_key(spec) not in CA._CLIENTS
    assert await ADAPTERS["acp"].teardown(d) is False  # idempotent


# ── health prober (PR4) ───────────────────────────────────────────────────────

import plugins.delegates.health as H  # noqa: E402


async def test_health_probe_all_populates_and_prunes(monkeypatch):
    H._HEALTH.clear()
    import plugins.delegates.store as store

    monkeypatch.setattr(
        store, "merged_delegates", lambda: [{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}]
    )

    async def fake_probe(d):
        return {"ok": True, "latency_ms": 5, "detail": "ok"}

    monkeypatch.setattr(ADAPTERS["openai"], "probe", fake_probe)
    await H._probe_all()
    assert H._HEALTH["opus"]["ok"] is True
    assert "checked_at" in H._HEALTH["opus"]

    # delegate removed → pruned on the next sweep
    monkeypatch.setattr(store, "merged_delegates", lambda: [])
    await H._probe_all()
    assert "opus" not in H._HEALTH


async def test_health_probe_records_failure(monkeypatch):
    H._HEALTH.clear()
    import plugins.delegates.store as store

    monkeypatch.setattr(
        store, "merged_delegates", lambda: [{"name": "p", "type": "acp", "command": "proto", "workdir": "/tmp"}]
    )

    async def boom(d):
        raise RuntimeError("nope")

    monkeypatch.setattr(ADAPTERS["acp"], "probe", boom)
    await H._probe_all()
    assert H._HEALTH["p"]["ok"] is False and "nope" in H._HEALTH["p"]["error"]


# ── per-delegate backoff (remote-member health robustness) ─────────────────────


def test_backoff_delay_grows_then_caps():
    # healthy → base; each consecutive failure doubles; pinned at the ceiling.
    assert H._backoff_delay(0) == H._BACKOFF_BASE_S
    assert H._backoff_delay(1) == H._BACKOFF_BASE_S * 2
    assert H._backoff_delay(2) == H._BACKOFF_BASE_S * 4
    seq = [H._backoff_delay(i) for i in range(0, 8)]
    assert seq == sorted(seq)  # monotonically non-decreasing
    assert max(seq) == H._BACKOFF_MAX_S  # eventually caps
    assert H._backoff_delay(100) == H._BACKOFF_MAX_S  # stays capped


async def test_health_backoff_skips_until_due_then_resets_on_success(monkeypatch):
    H._HEALTH.clear()
    H._FAILURES.clear()
    H._NEXT_DUE.clear()
    import plugins.delegates.store as store

    monkeypatch.setattr(
        store, "merged_delegates", lambda: [{"name": "p", "type": "acp", "command": "proto", "workdir": "/tmp"}]
    )
    calls = {"n": 0}
    outcome = {"ok": False}

    async def probe(d):
        calls["n"] += 1
        return {"ok": outcome["ok"]}

    monkeypatch.setattr(ADAPTERS["acp"], "probe", probe)

    # t=0: first probe FAILS → backed off to base*2 out.
    await H._probe_all(now=0.0)
    assert calls["n"] == 1 and H._FAILURES["p"] == 1
    assert H._NEXT_DUE["p"] == H._backoff_delay(1)  # 0 + base*2

    # not yet due → skipped (a flaky peer isn't hammered every tick).
    await H._probe_all(now=H._backoff_delay(1) - 1.0)
    assert calls["n"] == 1

    # exactly due → re-probed, fails again → window widens further.
    due1 = H._NEXT_DUE["p"]
    await H._probe_all(now=due1)
    assert calls["n"] == 2 and H._FAILURES["p"] == 2
    assert H._NEXT_DUE["p"] == due1 + H._backoff_delay(2)

    # success resets to the base cadence and clears the failure count.
    outcome["ok"] = True
    due2 = H._NEXT_DUE["p"]
    await H._probe_all(now=due2)
    assert calls["n"] == 3 and "p" not in H._FAILURES
    assert H._NEXT_DUE["p"] == due2 + H._BACKOFF_BASE_S


async def test_health_backoff_state_pruned_with_delegate(monkeypatch):
    H._HEALTH.clear()
    H._FAILURES.clear()
    H._NEXT_DUE.clear()
    import plugins.delegates.store as store

    monkeypatch.setattr(
        store, "merged_delegates", lambda: [{"name": "p", "type": "acp", "command": "proto", "workdir": "/tmp"}]
    )

    async def boom(d):
        raise RuntimeError("nope")

    monkeypatch.setattr(ADAPTERS["acp"], "probe", boom)
    await H._probe_all(now=0.0)
    assert "p" in H._FAILURES and "p" in H._NEXT_DUE

    monkeypatch.setattr(store, "merged_delegates", lambda: [])
    await H._probe_all(now=1.0)
    assert "p" not in H._HEALTH and "p" not in H._FAILURES and "p" not in H._NEXT_DUE
