"""Tests for infra.publish.client — the hosted-publish wire client (#2179 P2, #2683).

Mirrors tests/test_secrets_hydration.py's InfisicalProvider coverage: httpx.MockTransport
stands in for the (not-yet-existing) hosted service, so every failure mode is exercised
without a real network call or a real #2685 to point at.
"""

from __future__ import annotations

import httpx

from infra.publish.client import PublishErrorKind, publish_bundle


def _mock(handler):
    return httpx.MockTransport(handler)


def test_not_configured_never_makes_a_network_call():
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not attempt a request with no endpoint configured")

    out = publish_bundle(b"zip-bytes", endpoint_url="", transport=_mock(_boom))
    assert out.ok is False
    assert out.error_kind is PublishErrorKind.NOT_CONFIGURED
    assert "not configured" in out.error or "isn't configured" in out.error


def test_not_configured_tolerates_whitespace_only_url():
    out = publish_bundle(b"x", endpoint_url="   ", transport=_mock(lambda r: httpx.Response(200)))
    assert out.error_kind is PublishErrorKind.NOT_CONFIGURED


def test_happy_path_returns_the_public_link():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={"public_url": "https://protolabs.studio/c/abc123", "revoke_token": "rvk_1", "expires_at": None},
        )

    out = publish_bundle(b"zip-bytes", endpoint_url="https://hosted.test/bundles", transport=_mock(handler))
    assert seen == {"method": "POST", "content_type": "application/zip", "body": b"zip-bytes"}
    assert out.ok is True
    assert out.public_url == "https://protolabs.studio/c/abc123"
    assert out.revoke_token == "rvk_1"
    assert out.expires_at is None


def test_quota_rejection_surfaces_the_response_detail():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "daily publish quota exceeded"})

    out = publish_bundle(b"x", endpoint_url="https://hosted.test/bundles", transport=_mock(handler))
    assert out.ok is False
    assert out.error_kind is PublishErrorKind.REJECTED
    assert out.error == "daily publish quota exceeded"


def test_too_large_rejection_without_a_detail_body_falls_back_to_a_generic_reason():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(413)

    out = publish_bundle(b"x", endpoint_url="https://hosted.test/bundles", transport=_mock(handler))
    assert out.error_kind is PublishErrorKind.REJECTED
    assert "too large" in out.error


def test_server_error_is_bad_response_not_rejected():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    out = publish_bundle(b"x", endpoint_url="https://hosted.test/bundles", transport=_mock(handler))
    assert out.error_kind is PublishErrorKind.BAD_RESPONSE
    assert "500" in out.error


def test_missing_public_url_in_a_200_is_bad_response():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"revoke_token": "t"})

    out = publish_bundle(b"x", endpoint_url="https://hosted.test/bundles", transport=_mock(handler))
    assert out.ok is False
    assert out.error_kind is PublishErrorKind.BAD_RESPONSE


def test_timeout_is_reported_as_timeout_not_network():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    out = publish_bundle(b"x", endpoint_url="https://hosted.test/bundles", timeout_seconds=3.0, transport=_mock(handler))
    assert out.error_kind is PublishErrorKind.TIMEOUT
    assert "3s" in out.error


def test_connect_failure_is_network():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    out = publish_bundle(b"x", endpoint_url="https://hosted.test/bundles", transport=_mock(handler))
    assert out.error_kind is PublishErrorKind.NETWORK


def test_never_raises_on_an_unexpected_exception():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("something the client never anticipated")

    out = publish_bundle(b"x", endpoint_url="https://hosted.test/bundles", transport=_mock(handler))
    assert out.ok is False
    assert out.error_kind is PublishErrorKind.INTERNAL
