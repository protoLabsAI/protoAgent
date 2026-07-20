"""Fleet service token (ADR 0089) — graph/fleet/service_token.py.

The instance's internal, loopback-only credential: generated once and persisted beside the
fleet registry on a hub, delivered to members by env. Covers env-wins (a member), read-or-
create + persistence + 0600 (a hub), stability, and the process cache.
"""

from __future__ import annotations

import pytest

from graph.fleet import service_token as st


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    """Clear the process cache and point the token file at a temp workspaces root."""
    monkeypatch.setattr(st, "_cached", [None])
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path))
    monkeypatch.delenv(st.ENV_VAR, raising=False)
    yield


def test_env_var_wins_and_never_touches_disk(monkeypatch, tmp_path):
    monkeypatch.setenv(st.ENV_VAR, "injected-by-the-hub")
    assert st.resolve_service_token() == "injected-by-the-hub"
    # A member reads the env var; it must not create a file under its own (empty) root.
    assert not (tmp_path / ".fleet-token").exists()


def test_reads_or_creates_and_persists_0600(tmp_path):
    token = st.resolve_service_token()
    path = tmp_path / ".fleet-token"
    assert token and path.read_text("utf-8").strip() == token
    assert (path.stat().st_mode & 0o777) == 0o600  # a service credential, even on loopback


def test_second_process_reads_the_same_token(monkeypatch, tmp_path):
    first = st.resolve_service_token()
    monkeypatch.setattr(st, "_cached", [None])  # simulate a fresh process, same file
    assert st.resolve_service_token() == first


def test_cached_within_process(monkeypatch):
    first = st.resolve_service_token()
    # Delete the file; a cached process must not regenerate.
    (st._token_path()).unlink()
    assert st.resolve_service_token() == first


def test_ephemeral_when_root_unwritable(monkeypatch, tmp_path):
    # An unwritable instance root must not crash boot — fall back to a process token.
    def boom(*a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(st.Path, "mkdir", boom)
    (tmp_path / ".fleet-token").unlink(missing_ok=True)
    token = st.resolve_service_token()
    assert token  # still get a usable token
    assert not (tmp_path / ".fleet-token").exists()
