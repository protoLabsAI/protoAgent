"""The frozen desktop sidecar must point native TLS clients at the bundled certifi
CA bundle. ``ddgs`` (behind ``web_search``) verifies over OpenSSL via ``primp``, whose
OS trust-store discovery doesn't resolve in a PyInstaller onefile build — so without
this, DuckDuckGo search fails ``CERTIFICATE_VERIFY_FAILED`` in the desktop app while
httpx calls (github/gateway) still work. Frozen-only, never clobbers an override."""

from __future__ import annotations

import os
import sys

import certifi

import server

_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def test_source_checkout_is_a_noop(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)
    server._ensure_ca_bundle_env()
    for v in _VARS:
        assert v not in os.environ  # dev/source TLS discovery already works — untouched


def test_frozen_points_clients_at_bundled_certifi(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)
    server._ensure_ca_bundle_env()
    ca = certifi.where()
    assert ca and os.path.exists(ca)
    for v in _VARS:
        assert os.environ[v] == ca


def test_frozen_never_clobbers_operator_override(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/corp-ca.pem")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    server._ensure_ca_bundle_env()
    # An explicit operator SSL_CERT_FILE (e.g. a corporate MITM bundle) is preserved;
    # the unset sibling still gets the bundled default.
    assert os.environ["SSL_CERT_FILE"] == "/custom/corp-ca.pem"
    assert os.environ["REQUESTS_CA_BUNDLE"] == certifi.where()
