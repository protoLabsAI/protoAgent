"""Tests for A2A peer federation tools."""

import pytest

from tools.peer_tools import _resolve_peer, get_peer_tools, list_env_peers


def _tools():
    return {t.name: t for t in get_peer_tools()}


def test_list_env_peers(monkeypatch):
    monkeypatch.setenv("PEER_ALICE_URL", "https://alice.example")
    monkeypatch.setenv("PEER_ALICE_TOKEN", "secret")
    monkeypatch.setenv("PEER_BOB_URL", "https://bob.example")
    peers = {p["handle"]: p for p in list_env_peers()}
    assert peers["alice"]["url"] == "https://alice.example"
    assert peers["alice"]["has_token"] is True
    assert peers["bob"]["has_token"] is False


def test_resolve_peer(monkeypatch):
    monkeypatch.setenv("PEER_ALICE_URL", "https://alice.example")
    monkeypatch.setenv("PEER_ALICE_TOKEN", "tok")
    assert _resolve_peer("alice") == ("https://alice.example", "tok")
    assert _resolve_peer("nope") == (None, None)
    assert _resolve_peer("bad handle!") == (None, None)


@pytest.mark.asyncio
async def test_peer_consult_not_configured():
    out = await _tools()["peer_consult"].ainvoke({"name": "ghost", "message": "hi"})
    assert out.startswith("Error:") and "not configured" in out


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, "err"

    def json(self):
        return self._p


class _FakeClient:
    """Async-context client returning queued responses per .post call."""
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        return self._responses.pop(0)


def _task(state, text=None):
    art = {"artifacts": [{"parts": [{"kind": "text", "text": text}]}]} if text else {}
    return {"jsonrpc": "2.0", "result": {"id": "t1", "status": {"state": state}, **art}}


@pytest.mark.asyncio
async def test_peer_consult_inline_reply(monkeypatch):
    monkeypatch.setenv("PEER_ALICE_URL", "https://alice.example")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient([_Resp(_task("completed", "the answer"))]))
    out = await _tools()["peer_consult"].ainvoke({"name": "alice", "message": "q"})
    assert out == "[alice] the answer"


@pytest.mark.asyncio
async def test_peer_consult_polls_async_task(monkeypatch):
    monkeypatch.setenv("PEER_ALICE_URL", "https://alice.example")
    monkeypatch.setattr("tools.peer_tools._POLL_INTERVAL_S", 0)
    import httpx
    # send → submitted (no text); first tasks/get → working; second → completed.
    responses = [_Resp(_task("submitted")), _Resp(_task("working")), _Resp(_task("completed", "done"))]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(responses))
    out = await _tools()["peer_consult"].ainvoke({"name": "alice", "message": "q"})
    assert out == "[alice] done"


@pytest.mark.asyncio
async def test_peer_consult_http_error(monkeypatch):
    monkeypatch.setenv("PEER_ALICE_URL", "https://alice.example")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient([_Resp({}, status=500)]))
    out = await _tools()["peer_consult"].ainvoke({"name": "alice", "message": "q"})
    assert out.startswith("Error:") and "failed" in out


def test_get_all_tools_includes_peers_only_when_configured(monkeypatch):
    from tools.lg_tools import get_all_tools
    names = {t.name for t in get_all_tools()}
    assert "peer_consult" not in names  # no peers configured
    monkeypatch.setenv("PEER_ALICE_URL", "https://alice.example")
    names2 = {t.name for t in get_all_tools()}
    assert "peer_consult" in names2 and "peer_list" in names2
