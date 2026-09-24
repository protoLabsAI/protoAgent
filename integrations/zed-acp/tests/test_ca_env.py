"""A stale inherited CA env var must not crash the shim at startup.

Zed launched by a PyInstaller-frozen protoAgent server inherited that server's
``SSL_CERT_FILE=…/_MEI…/certifi/cacert.pem``; the server restarted, the ``_MEI`` dir
was deleted, and every protoagent-acp Zed started died in httpx's SSL-context setup
with ``FileNotFoundError``."""

from __future__ import annotations

import logging
import os

import httpx
import pytest

from protoagent_acp.a2a import A2AClient, drop_stale_ca_env


@pytest.fixture(autouse=True)
def _clean_ca_env(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


async def test_missing_cert_file_is_dropped_and_client_builds(monkeypatch, tmp_path, caplog):
    stale = str(tmp_path / "_MEIgone" / "certifi" / "cacert.pem")
    monkeypatch.setenv("SSL_CERT_FILE", stale)
    with pytest.raises(FileNotFoundError):  # the bug, as httpx hits it on its own
        httpx.AsyncClient()
    with caplog.at_level(logging.WARNING, logger="protoagent_acp.a2a"):
        client = A2AClient("https://example.invalid")
    await client.aclose()
    assert "SSL_CERT_FILE" not in os.environ
    assert any("SSL_CERT_FILE" in r.getMessage() and stale in r.getMessage() for r in caplog.records)


def test_missing_cert_dir_is_dropped(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "nope"))
    assert drop_stale_ca_env() == ["SSL_CERT_DIR"]
    assert "SSL_CERT_DIR" not in os.environ


def test_existing_paths_are_kept(monkeypatch, tmp_path):
    import certifi

    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())  # e.g. an operator's corporate bundle
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    assert drop_stale_ca_env() == []
    assert os.environ["SSL_CERT_FILE"] == certifi.where()
    assert os.environ["SSL_CERT_DIR"] == str(tmp_path)


def test_unset_is_a_noop():
    assert drop_stale_ca_env() == []
