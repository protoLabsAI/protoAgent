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


def test_acp_parse_manage_git_fields():
    # Managed git (ADR 0076): off by default; fields parse with sane fallbacks.
    d = ADAPTERS["acp"].parse({"name": "c", "type": "acp", "command": "proto", "workdir": "/tmp"})
    assert d.manage_git is False and d.base_branch == "main" and d.branch_prefix == ""
    d = ADAPTERS["acp"].parse(
        {
            "name": "c",
            "type": "acp",
            "command": "proto",
            "workdir": "/tmp",
            "manage_git": "true",
            "base_branch": "  develop ",
            "branch_prefix": "wt-1",
        }
    )
    assert d.manage_git is True and d.base_branch == "develop" and d.branch_prefix == "wt-1"


def test_acp_parse_claude_code_alias():
    # `claude-code` is a convenience alias for the claude-agent-acp adapter (#1116):
    # the operator's intuitive name maps to the real binary, with no launch args.
    d = ADAPTERS["acp"].parse(
        {"name": "cc", "type": "acp", "command": "claude-code", "args": ["--stray"], "workdir": "/tmp"}
    )
    assert d.command == "claude-agent-acp" and d.args == []


def test_acp_parse_env_remove_and_flows_to_spec():
    # Subtractive env seam (#2117): env_remove parses to a clean list (stringified,
    # blanks dropped) and rides the client spec — so a caller that scopes the delegate
    # per-dispatch via dataclasses.replace gets the seam for free.
    d = ADAPTERS["acp"].parse(
        {
            "name": "coder",
            "type": "acp",
            "command": "proto",
            "workdir": "/tmp",
            "env_remove": ["PROTOAGENT_", "A2A_AUTH_TOKEN", 123, ""],
        }
    )
    assert d.env_remove == ["PROTOAGENT_", "A2A_AUTH_TOKEN", "123"]
    assert ADAPTERS["acp"]._spec(d)["env_remove"] == ["PROTOAGENT_", "A2A_AUTH_TOKEN", "123"]
    # A delegate WITHOUT env_remove defaults to [] and carries [] on the spec (no regression).
    d2 = ADAPTERS["acp"].parse({"name": "c", "type": "acp", "command": "proto", "workdir": "/tmp"})
    assert d2.env_remove == [] and ADAPTERS["acp"]._spec(d2)["env_remove"] == []


async def test_acp_probe_bare_claude_hints_the_adapter():
    # `claude` is on PATH but has no native ACP mode — the probe must steer to the
    # adapter rather than show green (the false-green the old PATH check gave, #1116).
    d = ADAPTERS["acp"].parse({"name": "x", "type": "acp", "command": "claude", "workdir": "/tmp"})
    res = await ADAPTERS["acp"].probe(d)
    assert res["ok"] is False and "claude-agent-acp" in res["error"]


async def test_acp_probe_fails_when_handshake_fails(monkeypatch, tmp_path):
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
    d = ADAPTERS["acp"].parse({"name": "x", "type": "acp", "command": sys.executable, "workdir": str(tmp_path)})
    res = await ADAPTERS["acp"].probe(d)
    assert res["ok"] is False and "handshake failed" in res["error"]


async def test_acp_probe_ok_on_successful_handshake(monkeypatch, tmp_path):
    import sys

    from plugins.coding_agent.acp_client import AcpClient

    async def _ok(self):
        self._protocol_version = 1

    async def _noop(self):
        pass

    monkeypatch.setattr(AcpClient, "handshake", _ok)
    monkeypatch.setattr(AcpClient, "close", _noop)
    d = ADAPTERS["acp"].parse({"name": "x", "type": "acp", "command": sys.executable, "workdir": str(tmp_path)})
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
    reg = DelegateRegistry(
        [{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m", "description": "a model"}]
    )
    assert reg.roster() == [{"name": "opus", "type": "openai", "description": "a model", "url": "https://g/v1"}]


def test_list_agents_lists_roster_with_health(monkeypatch):
    r = _register(
        [{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m", "description": "a model"}], monkeypatch
    )
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


# ── delegate_to background=True (ADR 0050) ─────────────────────────────────────


class _FakeBgManager:
    """Records spawn_work calls; returns a fixed job id WITHOUT running the work — so the
    test asserts the detach happened, not the delegate dispatch."""

    def __init__(self):
        self.calls = []

    async def spawn_work(self, **kwargs):
        self.calls.append(kwargs)
        return "job-abc123"


async def test_delegate_to_background_spawns_detached_job(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}], monkeypatch)
    tool = r.tools[0]
    # Wire a fake BackgroundManager onto the runtime state the tool reaches for.
    import runtime.state as rs

    fake = _FakeBgManager()
    monkeypatch.setattr(rs.STATE, "background_mgr", fake, raising=False)
    # dispatch must NOT run inline — the fake spawn_work never calls work.
    monkeypatch.setattr(DelegateRegistry, "dispatch", _unexpected_dispatch)

    out = await tool.ainvoke({"target": "opus", "query": "build the thing", "background": True})
    assert "job-abc123" in out and "background" in out.lower()
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["kind"] == "delegate"
    assert "opus" in call["description"] and call["detail"] == "build the thing"
    # The queued work, when awaited, dispatches to the delegate.
    monkeypatch.setattr(DelegateRegistry, "dispatch", _fake_dispatch)
    assert await call["work"]() == "dispatched:build the thing"


async def test_delegate_to_background_unknown_fails_fast(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}], monkeypatch)
    tool = r.tools[0]
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "background_mgr", _FakeBgManager(), raising=False)
    out = await tool.ainvoke({"target": "nope", "query": "hi", "background": True})
    assert "unknown delegate" in out  # no orphan job for a bad target


async def test_delegate_to_background_falls_back_inline_without_manager(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}], monkeypatch)
    tool = r.tools[0]
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "background_mgr", None, raising=False)
    monkeypatch.setattr(DelegateRegistry, "dispatch", _fake_dispatch)
    # No manager → background degrades to a normal inline dispatch, never worse than sync.
    out = await tool.ainvoke({"target": "opus", "query": "quick q", "background": True})
    assert out == "dispatched:quick q"


async def _fake_dispatch(self, name, query, *, item_id=None):
    return f"dispatched:{query}"


async def _unexpected_dispatch(self, name, query, *, item_id=None):
    raise AssertionError("dispatch must not run inline when a background job is spawned")


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


def _openai_delegate():
    return ADAPTERS["openai"].parse({"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"})


async def test_openai_http_error_names_the_delegate(monkeypatch):
    """An unattributed `HTTP 401` tells an agent that fanned out to several delegates
    neither which one failed nor why."""
    import httpx

    class _Failing(_FakeClient):
        async def post(self, url, **kw):
            return _FakeResp({"error": {"message": "invalid api key"}}, status=401)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Failing(None))
    with pytest.raises(DelegateError) as ei:
        await ADAPTERS["openai"].dispatch(_openai_delegate(), "q")
    msg = str(ei.value)
    assert "opus" in msg and "401" in msg and "invalid api key" in msg


async def test_openai_transport_error_says_unreachable(monkeypatch):
    """Previously a ConnectError escaped the adapter and reached the tool as a raw
    exception type, so `unreachable` and `the model refused` read identically."""
    import httpx

    class _Refusing(_FakeClient):
        async def post(self, url, **kw):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Refusing(None))
    with pytest.raises(DelegateError) as ei:
        await ADAPTERS["openai"].dispatch(_openai_delegate(), "q")
    assert "opus" in str(ei.value) and "unreachable" in str(ei.value)


async def test_openai_inline_error_object_beats_keyerror(monkeypatch):
    """An OpenAI-compatible endpoint that refuses in-band answers 200 with an `error`
    object and no `choices`; that used to surface as `unexpected response shape:
    'choices'`, hiding the endpoint's own explanation."""
    import httpx

    payload = {"error": {"message": "context length exceeded", "type": "invalid_request_error"}}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(payload))
    with pytest.raises(DelegateError) as ei:
        await ADAPTERS["openai"].dispatch(_openai_delegate(), "q")
    assert "context length exceeded" in str(ei.value)


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


def test_healthy_cadence_relaxes_only_when_nobody_is_watching():
    """The existing backoff only slowed FAILING delegates; healthy ones paid the base
    cadence forever, which on 7 ACP delegates is ~5,000 subprocess launches a day for a
    status badge (#2542)."""
    # Someone is looking → unchanged, no matter how long it's been healthy.
    assert H._backoff_delay(0, 1, observed=True) == H._BACKOFF_BASE_S
    assert H._backoff_delay(0, 50, observed=True) == H._BACKOFF_BASE_S

    # Nobody looking → the cadence relaxes as the delegate proves itself, then pins.
    relaxed = [H._backoff_delay(0, n, observed=False) for n in range(1, 12)]
    assert relaxed == sorted(relaxed)
    assert relaxed[0] == H._BACKOFF_BASE_S  # first success is still base
    assert max(relaxed) == H._HEALTHY_MAX_S
    # The acceptance criterion: ≥5× fewer probes per hour on an idle instance.
    assert H._HEALTHY_MAX_S / H._BACKOFF_BASE_S >= 5

    # A failure outranks any success streak — it snaps straight back to the fail curve.
    assert H._backoff_delay(1, 50, observed=False) == H._BACKOFF_BASE_S * 2


def test_reading_the_snapshot_counts_as_being_watched(monkeypatch):
    """`health_snapshot` IS the panel rendering the badge, so it doubles as the
    "a human is here" signal — the surface knows, without extra plumbing."""
    H._last_observed = 0.0
    assert H._observed() is False

    H.health_snapshot()

    assert H._observed() is True


async def test_a_steady_delegate_is_probed_less_often_when_unobserved(monkeypatch):
    H._HEALTH.clear()
    H._FAILURES.clear()
    H._SUCCESSES.clear()
    H._NEXT_DUE.clear()
    H._last_observed = 0.0  # nobody has opened the panel
    import plugins.delegates.store as store

    monkeypatch.setattr(
        store, "merged_delegates", lambda: [{"name": "p", "type": "acp", "command": "proto", "workdir": "/tmp"}]
    )
    calls = {"n": 0}

    async def probe(d):
        calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(ADAPTERS["acp"], "probe", probe)

    # Walk the delegate forward through repeated successes; each gap is its own cadence.
    now = 0.0
    gaps = []
    for _ in range(6):
        await H._probe_all(now=now)
        gaps.append(H._NEXT_DUE["p"] - now)
        now = H._NEXT_DUE["p"]

    assert calls["n"] == 6
    assert gaps[0] == H._BACKOFF_BASE_S  # unchanged at first
    assert gaps[-1] == H._HEALTHY_MAX_S  # settled at the relaxed steady state
    assert gaps == sorted(gaps)

    # It is still SKIPPED before it's due — the relaxed window is real, not cosmetic.
    await H._probe_all(now=now - 1.0)
    assert calls["n"] == 6


async def test_a_failure_snaps_a_relaxed_delegate_back(monkeypatch):
    """Relaxing must not cost responsiveness where it matters: the moment a steady
    delegate fails, it returns to the tight failure cadence."""
    H._HEALTH.clear()
    H._FAILURES.clear()
    H._SUCCESSES.clear()
    H._NEXT_DUE.clear()
    H._last_observed = 0.0
    import plugins.delegates.store as store

    monkeypatch.setattr(
        store, "merged_delegates", lambda: [{"name": "p", "type": "acp", "command": "proto", "workdir": "/tmp"}]
    )
    outcome = {"ok": True}

    async def probe(d):
        return {"ok": outcome["ok"]}

    monkeypatch.setattr(ADAPTERS["acp"], "probe", probe)

    now = 0.0
    for _ in range(6):
        await H._probe_all(now=now)
        now = H._NEXT_DUE["p"]
    assert H._SUCCESSES["p"] == 6

    outcome["ok"] = False
    await H._probe_all(now=now)

    assert "p" not in H._SUCCESSES and H._FAILURES["p"] == 1
    assert H._NEXT_DUE["p"] - now == H._BACKOFF_BASE_S * 2


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


def test_is_secretish_matches_tokens_not_substrings():
    """Panel on #2150: 'auth' inside 'author' must not classify GIT_AUTHOR_NAME as a
    secret (it was being auto-routed to secrets.yaml); real secret names still match."""
    from plugins.delegates.adapters import is_secretish

    assert not is_secretish("GIT_AUTHOR_NAME")
    assert not is_secretish("AUTHORITATIVE_DNS")
    for good in ("AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "API_KEY", "MY_PASSWORD", "OAUTH_CLIENT", "GH_BEARER"):
        assert is_secretish(good), good


# ── last-dispatch outcome (item 6: health ≠ "the work went through") ──────────


@pytest.fixture(autouse=True)
def _clean_dispatch_status():
    from plugins.delegates import status

    status.reset()
    yield
    status.reset()


def _stub_registry(monkeypatch, behaviour):
    """A registry with one acp delegate whose adapter dispatch does `behaviour`."""
    from plugins.delegates.adapters import ADAPTERS

    reg = DelegateRegistry([{"name": "codex", "type": "acp", "command": "x", "workdir": "/tmp"}])

    async def _dispatch(d, query, *, timeout=None, item_id=None):
        return behaviour()

    monkeypatch.setattr(ADAPTERS["acp"], "dispatch", _dispatch)
    return reg


async def test_dispatch_failure_is_recorded_for_the_panel(monkeypatch):
    """The reported failure mode: an acp probe only runs the ACP handshake, so a coder
    that launches but fails every session shows a green health dot. The last dispatch is
    the signal that disagrees."""
    from plugins.delegates import status

    def boom():
        raise DelegateError("delegate 'codex' (codex-acp): Internal error (JSON-RPC -32603): 429")

    reg = _stub_registry(monkeypatch, boom)
    with pytest.raises(DelegateError):
        await reg.dispatch("codex", "go")
    last = status.snapshot()["codex"]
    assert last["ok"] is False
    assert "-32603" in last["error"]
    assert last["at"] > 0


async def test_dispatch_success_clears_a_stale_failure(monkeypatch):
    """A failure that never clears reads as current and is worse than showing nothing."""
    from plugins.delegates import status

    status.record_failure("codex", "old news")
    reg = _stub_registry(monkeypatch, lambda: "done")
    assert await reg.dispatch("codex", "go") == "done"
    assert status.snapshot()["codex"]["ok"] is True
    assert "error" not in status.snapshot()["codex"]


async def test_cancelled_dispatch_is_not_a_delegate_failure(monkeypatch):
    """An operator hitting stop says nothing about the delegate — recording it would put
    a red mark on a healthy coder every time a turn is interrupted."""
    import asyncio

    from plugins.delegates import status

    def cancel():
        raise asyncio.CancelledError()

    reg = _stub_registry(monkeypatch, cancel)
    with pytest.raises(asyncio.CancelledError):
        await reg.dispatch("codex", "go")
    assert "codex" not in status.snapshot()


async def test_unknown_delegate_records_nothing(monkeypatch):
    """No delegate to attribute the failure to — a phantom row would be a bug, not a hint."""
    from plugins.delegates import status

    reg = DelegateRegistry([])
    with pytest.raises(DelegateError):
        await reg.dispatch("ghost", "go")
    assert status.snapshot() == {}


def test_status_prunes_deleted_delegates_and_caps_the_error():
    from plugins.delegates import status

    status.record_failure("gone", "x" * 5000)
    status.record_success("stays")
    assert len(status.snapshot()["gone"]["error"]) <= 400
    status.prune({"stays"})
    assert set(status.snapshot()) == {"stays"}


def test_list_payload_exposes_last_dispatch(monkeypatch):
    """The panel reads this off /api/delegates alongside `health`."""
    from plugins.delegates import api, status
    from plugins.delegates import store as S

    monkeypatch.setattr(
        S, "read_delegates_raw", lambda: [{"name": "codex", "type": "acp", "command": "x", "workdir": "/tmp"}]
    )
    monkeypatch.setattr(S, "secret_overlay", lambda: {})
    monkeypatch.setattr(S, "env_secret_values", lambda overlay, name: {})
    status.record_failure("codex", "Internal error (JSON-RPC -32603)")
    row = api._list_payload()["delegates"][0]
    assert row["last_dispatch"]["ok"] is False
    assert "-32603" in row["last_dispatch"]["error"]


# ── field tiers: the form asks for what matters first ────────────────────────


def test_every_type_has_a_workable_primary_tier():
    """The console collapses `advanced` fields. The acp form was the complaint — 10 fields
    flat, most of them defaults nobody changes. Assert the split stays sane per type rather
    than pinning exact key lists, which would make adding a field a two-file chore."""
    for typ, adapter in ADAPTERS.items():
        fields = adapter.config_schema()
        primary = [f for f in fields if not f.advanced]
        assert primary, f"{typ}: every type needs at least one primary field"
        assert len(primary) <= 4, f"{typ}: primary tier has grown to {len(primary)} — re-tier it"
        # A required field is by definition not optional-with-a-sane-default.
        assert all(not f.advanced for f in fields if f.required), f"{typ}: a required field is hidden"


def test_env_editor_is_advanced_on_every_type():
    """The env editor is the largest control on the form (rows + secret toggles + the
    removal list) and most delegates never set one."""
    for typ, adapter in ADAPTERS.items():
        env = [f for f in adapter.config_schema() if f.kind == "envmap"]
        assert env, f"{typ}: expected an env field"
        assert all(f.advanced for f in env), f"{typ}: env should be advanced"


def test_acp_keeps_permissions_primary():
    """`permissions` governs whether the coding agent may run shell commands and delete
    files in its workdir. It has a default like the other advanced fields, but hiding a
    security control behind a collapsed section is a different kind of decision."""
    acp = {f.key: f for f in ADAPTERS["acp"].config_schema()}
    assert acp["permissions"].advanced is False
    for key in ("command", "args", "workdir"):
        assert acp[key].advanced is False, f"{key} must stay visible"
    for key in ("timeout_s", "manage_git", "base_branch", "branch_prefix", "confirm"):
        assert acp[key].advanced is True, f"{key} should be advanced"


def test_field_tier_is_serialized_to_the_console():
    """The form reads this off /api/delegate-types; without it in as_dict() the console
    silently renders everything inline."""
    for spec in delegate_types():
        assert all("advanced" in f for f in spec["fields"]), spec["type"]


# ── #2352/#2363: an interrupted coder reply must not look like a finished one ──


class _StopReasonClient:
    """Minimal stand-in for the pooled ``AcpClient``: returns a canned reply and the
    ``stopReason`` the real client records on every turn."""

    def __init__(self, reply: str, stop_reason: str | None):
        self._reply = reply
        self.last_stop_reason = stop_reason
        self._permission = None

    async def prompt(self, query, timeout=None):
        return self._reply


def _acp_delegate():
    from plugins.delegates.adapters import AcpAdapter

    return AcpAdapter().parse({"name": "andrew", "type": "acp", "command": "claude-code", "workdir": "/tmp"})


async def _dispatch_with(monkeypatch, reply: str, stop_reason: str | None) -> str:
    import plugins.coding_agent as coding_agent
    from plugins.delegates.adapters import AcpAdapter

    monkeypatch.setattr(coding_agent, "_client_for", lambda spec: _StopReasonClient(reply, stop_reason))
    monkeypatch.setattr(coding_agent, "_make_permission", lambda spec: None)
    return await AcpAdapter()._prompt(_acp_delegate(), "do the thing")


async def test_max_tokens_reply_is_marked_incomplete(monkeypatch):
    """The reported failure (#2352): the model stopped at its output-token limit, the
    adapter returned the partial text, and the orchestrator could not tell it apart from
    a finished answer. `AcpClient.last_stop_reason` already knew — nothing read it."""
    out = await _dispatch_with(monkeypatch, "Here is the plan, step 1", "max_tokens")
    assert "Here is the plan, step 1" in out, "the partial work must be preserved"
    assert "incomplete reply" in out
    assert "CUT OFF" in out


async def test_refusal_is_marked_and_says_not_to_retry_verbatim(monkeypatch):
    out = await _dispatch_with(monkeypatch, "I can't help with that.", "refusal")
    assert "incomplete reply" in out
    assert "decline again" in out


async def test_end_turn_reply_is_returned_untouched(monkeypatch):
    """A normal completion must not grow a scary marker — the common path stays clean."""
    out = await _dispatch_with(monkeypatch, "Done: added /healthz and the tests pass.", "end_turn")
    assert out == "Done: added /healthz and the tests pass."


async def test_missing_stop_reason_is_returned_untouched(monkeypatch):
    """An ACP agent that omits stopReason must not be treated as truncated."""
    out = await _dispatch_with(monkeypatch, "fine", None)
    assert out == "fine"


async def test_cancelled_is_not_marked(monkeypatch):
    """An operator who hit stop already knows — mirrors status.py's rule that a
    cancellation is not a delegate failure."""
    out = await _dispatch_with(monkeypatch, "partial", "cancelled")
    assert out == "partial"


async def test_empty_reply_at_the_limit_still_explains_itself(monkeypatch):
    """A coder that produced nothing before hitting the cap would otherwise hand back an
    empty string — the one case where silence is the whole message."""
    out = await _dispatch_with(monkeypatch, "   ", "max_tokens")
    assert out.startswith("[no reply —")
    assert "output-token limit" in out
