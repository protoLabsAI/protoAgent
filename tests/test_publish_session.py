"""Tests for server.chat's publish_preview / publish_session (#2179 P2, #2683) — the
orchestration layer above graph.chat_bundle: STATE plumbing, config → publish_bundle
wiring, and translating infra.publish's typed result into the route's outcome dict.

graph.chat_bundle itself is covered by tests/test_chat_bundle.py; these tests exercise
what's specific to this layer, with infra.publish.publish_bundle monkeypatched so nothing
here makes a real network call.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from infra.publish.client import PublishErrorKind, PublishResult
from runtime.state import STATE
from server.chat import publish_preview, publish_session


class _FakeSnapshot:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages

    async def aget_state(self, _config):
        return _FakeSnapshot(self._messages)


def _set_graph(monkeypatch, messages):
    monkeypatch.setattr(STATE, "graph", _FakeGraph(messages), raising=False)
    monkeypatch.setattr(STATE, "checkpointer", object(), raising=False)
    monkeypatch.setattr(STATE, "thread_id_resolver", None, raising=False)


# ── publish_preview ──────────────────────────────────────────────────────────────
def test_publish_preview_setup_required(monkeypatch):
    monkeypatch.setattr(STATE, "graph", None, raising=False)
    out = asyncio.run(publish_preview("s1"))
    assert out["found"] is False and out["reason"] == "setup"
    assert "Setup required" in out["message"]


def test_publish_preview_never_calls_publish_bundle(monkeypatch):
    """The whole point of preview: it builds the bundle but sends nothing anywhere."""
    _set_graph(monkeypatch, [HumanMessage(content="hi"), AIMessage(content="hello")])

    def _boom(*_a, **_k):
        raise AssertionError("preview must never call publish_bundle")

    monkeypatch.setattr("infra.publish.publish_bundle", _boom)
    out = asyncio.run(publish_preview("s1", title="My Chat"))
    assert out["found"] is True
    assert out["manifest"]["title"] == "My Chat"
    assert "2 message(s)" in out["message"]


def test_publish_preview_empty_thread(monkeypatch):
    _set_graph(monkeypatch, [])
    out = asyncio.run(publish_preview("s1"))
    assert out["found"] is False and out["reason"] == "empty_thread"
    assert "no messages" in out["message"]


# ── publish_session ──────────────────────────────────────────────────────────────
def test_publish_session_setup_required(monkeypatch):
    monkeypatch.setattr(STATE, "graph", None, raising=False)
    out = asyncio.run(publish_session("s1"))
    assert out["published"] is False and out["reason"] == "setup"


def test_publish_session_empty_thread_never_calls_publish_bundle(monkeypatch):
    _set_graph(monkeypatch, [])

    def _boom(*_a, **_k):
        raise AssertionError("must not attempt to publish an empty thread")

    monkeypatch.setattr("infra.publish.publish_bundle", _boom)
    out = asyncio.run(publish_session("s1"))
    assert out["published"] is False and out["reason"] == "empty_thread"


def test_publish_session_passes_config_to_publish_bundle(monkeypatch):
    _set_graph(monkeypatch, [HumanMessage(content="hi")])
    monkeypatch.setattr(
        STATE,
        "graph_config",
        SimpleNamespace(publish_endpoint_url="https://example.test/bundles", publish_timeout_seconds=7.0),
        raising=False,
    )
    seen = {}

    def _fake_publish_bundle(data, *, endpoint_url, timeout_seconds):
        seen["data_is_bytes"] = isinstance(data, bytes)
        seen["endpoint_url"] = endpoint_url
        seen["timeout_seconds"] = timeout_seconds
        return PublishResult(public_url="https://protolabs.studio/c/abc", revoke_token="rvk_1", expires_at=None)

    monkeypatch.setattr("infra.publish.publish_bundle", _fake_publish_bundle)
    out = asyncio.run(publish_session("s1"))
    assert seen == {
        "data_is_bytes": True,
        "endpoint_url": "https://example.test/bundles",
        "timeout_seconds": 7.0,
    }
    assert out["published"] is True
    assert out["public_url"] == "https://protolabs.studio/c/abc"
    assert out["revoke_token"] == "rvk_1"


def test_publish_session_not_configured_is_not_an_error(monkeypatch):
    """The empty-endpoint default must read as an expected state, not a crash."""
    _set_graph(monkeypatch, [HumanMessage(content="hi")])
    monkeypatch.setattr(
        STATE, "graph_config", SimpleNamespace(publish_endpoint_url="", publish_timeout_seconds=15.0), raising=False
    )

    def _fake_publish_bundle(_data, *, endpoint_url, timeout_seconds):
        return PublishResult(
            error="hosted publishing isn't configured — set publish.endpoint_url in Settings",
            error_kind=PublishErrorKind.NOT_CONFIGURED,
        )

    monkeypatch.setattr("infra.publish.publish_bundle", _fake_publish_bundle)
    out = asyncio.run(publish_session("s1"))
    assert out["published"] is False
    assert out["reason"] == "not_configured"
    assert "isn't configured" in out["message"]


def test_publish_session_surfaces_the_error_kind_and_report(monkeypatch):
    _set_graph(monkeypatch, [HumanMessage(content="hi")])
    monkeypatch.setattr(
        STATE, "graph_config", SimpleNamespace(publish_endpoint_url="https://x.test", publish_timeout_seconds=15.0), raising=False
    )

    def _fake_publish_bundle(*_a, **_k):
        return PublishResult(error="quota exceeded", error_kind=PublishErrorKind.REJECTED)

    monkeypatch.setattr("infra.publish.publish_bundle", _fake_publish_bundle)
    out = asyncio.run(publish_session("s1"))
    assert out["published"] is False
    assert out["reason"] == "rejected"
    assert "quota exceeded" in out["message"]


def test_publish_session_reports_redactions_and_artifact_notes_on_success(monkeypatch):
    _set_graph(
        monkeypatch,
        [HumanMessage(content="my key is sk-abcdefghijklmnopqrstuvwxyz123456")],
    )
    monkeypatch.setattr(
        STATE, "graph_config", SimpleNamespace(publish_endpoint_url="https://x.test", publish_timeout_seconds=15.0), raising=False
    )
    monkeypatch.setattr(
        "infra.publish.publish_bundle",
        lambda *_a, **_k: PublishResult(public_url="https://protolabs.studio/c/xyz", revoke_token="t", expires_at=None),
    )
    out = asyncio.run(publish_session("s1"))
    assert out["published"] is True
    assert out["redactions"] == ["openai-key"]


# ── #2684: publish_session records a link; revoke_published_link un-shares it ────────
def test_publish_session_records_a_published_link(monkeypatch):
    """The whole point of #2684 — a successful publish must be listable/revocable
    later, not just reported once in the response and forgotten."""
    from infra.publish import list_published_links

    _set_graph(monkeypatch, [HumanMessage(content="hi")])
    monkeypatch.setattr(
        STATE, "graph_config", SimpleNamespace(publish_endpoint_url="https://x.test", publish_timeout_seconds=15.0), raising=False
    )
    monkeypatch.setattr(
        "infra.publish.publish_bundle",
        lambda *_a, **_k: PublishResult(public_url="https://protolabs.studio/c/xyz", revoke_token="tok-1", expires_at=None),
    )
    out = asyncio.run(publish_session("s1", title="My chat"))
    assert out["link_id"] is not None

    links = list_published_links()
    assert len(links) == 1
    assert links[0]["id"] == out["link_id"]
    assert links[0]["public_url"] == "https://protolabs.studio/c/xyz"
    assert links[0]["title"] == "My chat"
    # The token itself must never appear in what a route could hand back to a browser.
    assert "revoke_token" not in links[0]


def test_publish_session_a_recording_failure_does_not_fail_the_publish(monkeypatch):
    """The bundle is ALREADY live on the hosted service by this point — a local disk
    hiccup recording it must not make a real success read back as a failure."""
    _set_graph(monkeypatch, [HumanMessage(content="hi")])
    monkeypatch.setattr(
        STATE, "graph_config", SimpleNamespace(publish_endpoint_url="https://x.test", publish_timeout_seconds=15.0), raising=False
    )
    monkeypatch.setattr(
        "infra.publish.publish_bundle",
        lambda *_a, **_k: PublishResult(public_url="https://protolabs.studio/c/xyz", revoke_token="tok-1", expires_at=None),
    )

    def _boom(**_k):
        raise OSError("disk full")

    monkeypatch.setattr("infra.publish.record_publish", _boom)
    out = asyncio.run(publish_session("s1"))
    assert out["published"] is True
    assert out["public_url"] == "https://protolabs.studio/c/xyz"
    assert out["link_id"] is None


def test_revoke_published_link_unknown_id(monkeypatch):
    from server.chat import revoke_published_link

    out = asyncio.run(revoke_published_link("nope"))
    assert out == {"ok": False, "reason": "not_found", "error": "unknown published link"}


def test_revoke_published_link_already_revoked_is_idempotent(monkeypatch):
    from infra.publish import mark_revoked, record_publish
    from server.chat import revoke_published_link

    link = record_publish(thread_id="t1", title="t", public_url="https://x/1", revoke_token="tok", expires_at=None)
    mark_revoked(link.id)

    def _boom(*_a, **_k):
        raise AssertionError("must not call the hosted service for an already-revoked link")

    monkeypatch.setattr("infra.publish.revoke_bundle", _boom)
    out = asyncio.run(revoke_published_link(link.id))
    assert out == {"ok": True}


def test_revoke_published_link_not_configured(monkeypatch):
    from infra.publish import record_publish
    from infra.publish.client import PublishErrorKind, RevokeResult
    from server.chat import revoke_published_link

    link = record_publish(thread_id="t1", title="t", public_url="https://x/1", revoke_token="tok", expires_at=None)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(publish_revoke_endpoint_url="", publish_timeout_seconds=15.0), raising=False)
    monkeypatch.setattr(
        "infra.publish.revoke_bundle",
        lambda *_a, **_k: RevokeResult(error="revocation isn't configured", error_kind=PublishErrorKind.NOT_CONFIGURED),
    )
    out = asyncio.run(revoke_published_link(link.id))
    assert out["ok"] is False
    assert out["reason"] == "not_configured"

    from infra.publish import get_link

    assert get_link(link.id).revoked_at is None  # NOT marked revoked — the hosted side never confirmed


def test_revoke_published_link_success_marks_it_revoked_locally(monkeypatch):
    from infra.publish import get_link, record_publish
    from infra.publish.client import RevokeResult
    from server.chat import revoke_published_link

    link = record_publish(thread_id="t1", title="t", public_url="https://x/1", revoke_token="tok-xyz", expires_at=None)
    seen = {}

    def _fake_revoke(token, *, endpoint_url, timeout_seconds):
        seen["token"] = token
        seen["endpoint_url"] = endpoint_url
        return RevokeResult(ok=True)

    monkeypatch.setattr(
        STATE,
        "graph_config",
        SimpleNamespace(publish_revoke_endpoint_url="https://x.test/revoke", publish_timeout_seconds=9.0),
        raising=False,
    )
    monkeypatch.setattr("infra.publish.revoke_bundle", _fake_revoke)
    out = asyncio.run(revoke_published_link(link.id))
    assert out == {"ok": True}
    assert seen == {"token": "tok-xyz", "endpoint_url": "https://x.test/revoke"}
    assert get_link(link.id).revoked_at is not None
