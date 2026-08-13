"""Published-link registry tests (#2179 P2, #2684).

Mirrors tests/test_device_pairing.py's isolation fixture (a throwaway instance root per
test) since the two stores share the same shape and the same reason to test it: both hold
a live credential (a revoke token here, a device token there) and both must survive a
corrupt file without taking the feature down with it.
"""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An `infra.publish.store` bound to a throwaway instance root."""
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-publish")

    import infra.paths

    infra.paths.reset_instance_paths()
    import infra.publish.store as mod

    importlib.reload(mod)
    yield mod
    infra.paths.reset_instance_paths()


def test_record_then_list(store):
    store.record_publish(
        thread_id="t1", title="My chat", public_url="https://x.test/c/1", revoke_token="tok1", expires_at=None
    )
    links = store.list_published_links()
    assert len(links) == 1
    assert links[0]["thread_id"] == "t1"
    assert links[0]["public_url"] == "https://x.test/c/1"
    assert links[0]["revoked_at"] is None


def test_list_never_includes_the_revoke_token(store):
    """The one property this store exists to protect — a token that made it into a
    console API response would sit in browser memory / devtools / logs indefinitely."""
    store.record_publish(
        thread_id="t1", title="t", public_url="https://x.test/c/1", revoke_token="super-secret-token", expires_at=None
    )
    dump = json.dumps(store.list_published_links())
    assert "super-secret-token" not in dump


def test_get_link_does_include_the_token_for_server_internal_use(store):
    link = store.record_publish(
        thread_id="t1", title="t", public_url="https://x.test/c/1", revoke_token="tok1", expires_at=None
    )
    fetched = store.get_link(link.id)
    assert fetched is not None
    assert fetched.revoke_token == "tok1"


def test_newest_first(store):
    a = store.record_publish(thread_id="a", title="a", public_url="https://x/a", revoke_token="ta", expires_at=None)
    b = store.record_publish(thread_id="b", title="b", public_url="https://x/b", revoke_token="tb", expires_at=None)
    ids = [l["id"] for l in store.list_published_links()]
    assert ids == [b.id, a.id]


def test_mark_revoked_flips_the_flag(store):
    link = store.record_publish(
        thread_id="t1", title="t", public_url="https://x.test/c/1", revoke_token="tok1", expires_at=None
    )
    assert store.mark_revoked(link.id) is True
    links = store.list_published_links()
    assert links[0]["revoked_at"] is not None


def test_mark_revoked_unknown_id_is_false(store):
    assert store.mark_revoked("nope") is False


def test_get_link_unknown_id_is_none(store):
    assert store.get_link("nope") is None


def test_corrupt_registry_is_treated_as_empty_not_a_crash(store):
    path = store._registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.list_published_links() == []
    # And a fresh publish still works — a corrupt file doesn't wedge the store.
    store.record_publish(thread_id="t1", title="t", public_url="https://x/1", revoke_token="tok", expires_at=None)
    assert len(store.list_published_links()) == 1


def test_a_hand_edited_partial_entry_is_skipped_not_fatal(store):
    path = store._registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": "bad", "thread_id": "t"}]), encoding="utf-8")  # missing required keys
    assert store.list_published_links() == []


def test_registry_file_is_owner_only(store):
    """Portable check (tests/privacy_asserts.py, #2412 phase 4) — POSIX mode bits are
    decorative on Windows (stat reports 0666 regardless of chmod), so a raw
    `st_mode == 0o600` assertion is wrong there, not just untested; this is the same
    helper agent_snapshot_import / media_store / config_io's credential files use."""
    from tests.privacy_asserts import assert_owner_only

    store.record_publish(thread_id="t1", title="t", public_url="https://x/1", revoke_token="tok", expires_at=None)
    assert_owner_only(store._registry_path())
