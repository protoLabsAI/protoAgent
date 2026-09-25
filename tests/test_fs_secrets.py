"""``tools.fs_secrets.is_secret_path`` — the code pane's secret-like-name deny list (ADR 0112)."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from tools.fs_secrets import is_secret_path


@pytest.mark.parametrize(
    "rel",
    [
        ".env",
        "app/.env",
        ".env.local",
        ".env.production",
        "config/.ENV",
        ".Env.Local",
        "certs/server.pem",
        "tls/private.KEY",
        "bundle.p12",
        "bundle.pfx",
        "release.keystore",
        "id_rsa",
        "id_rsa.pub",
        "keys/id_ed25519",
        "keys/ID_ED25519.pub",
        ".ssh/config",
        "home/.ssh/known_hosts",
        ".ssh",
        "config/secrets.yaml",
        "secrets.yml",
        "SECRETS.YAML",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "gcp/credentials-prod.json",
        "a\\b\\id_rsa",  # Windows separators
        "sub\\.ssh\\config",
    ],
)
def test_denied(rel):
    assert is_secret_path(rel), rel


@pytest.mark.parametrize(
    "rel",
    [
        ".env.example",
        ".env.sample",
        ".env.template",
        "app/.ENV.Example",
        "README.md",
        "src/env.py",
        "environment.ts",
        ".envrc.md",
        "keys.py",
        "docs/ssh.md",
        "my.ssh/readme",  # a component that merely contains ".ssh"
        "credentials.yaml",  # only credentials*.json is on the list
        "secrets.py",
        "monkey.json",
        "",
        ".",
    ],
)
def test_allowed(rel):
    assert is_secret_path(rel) is None, rel


def test_reason_names_the_rule():
    assert "pem" in is_secret_path("a/b.pem")
    assert ".ssh" in is_secret_path(".ssh/id")


def test_accepts_pure_paths():
    assert is_secret_path(PurePosixPath("x/.env"))
    assert is_secret_path(PurePosixPath("x/y.txt")) is None
