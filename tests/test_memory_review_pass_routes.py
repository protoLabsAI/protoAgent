"""Operator review pass over all memory tiers and explicit shared promotion."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge.memories import MemoryStore
from knowledge.shared_profile import SharedProfileStore
from operator_api.memory_routes import register_memory_routes
from runtime.state import STATE

pytestmark = pytest.mark.platform_sensitive
BASE = "/api/memory/memories"


@pytest.fixture
def review(tmp_path, monkeypatch):
    store = MemoryStore(str(tmp_path / "private.db"))
    shared = SharedProfileStore(tmp_path / "commons")
    monkeypatch.setattr(STATE, "memory_store", store)
    monkeypatch.setattr(STATE, "shared_profile_store", shared)
    app = FastAPI()
    register_memory_routes(app)
    with TestClient(app) as client:
        yield client, store, shared


def test_list_filters_pages_and_audit_fields(review):
    client, store, _ = review
    ids = [store.add(str(i), kind="fact", provenance="extracted", legacy_domain="preferences") for i in range(3)]
    other = store.add("other", kind="standing", provenance="operator", delivery="on_demand")
    store.set_review_state(ids[1], "rejected")
    with store._write() as db:
        db.execute("UPDATE memories SET expires_at='2000-01-01', invalidated_at='2001-01-01' WHERE id=?", (ids[0],))
        db.execute("UPDATE memories SET created_at='2020-01-01'")  # deterministic tie ordering
    data = client.get(BASE, params={"limit": 2, "offset": 1}).json()
    assert data["total"] == 4
    assert data["memories"] == [asdict(store.get(mid)) for mid in reversed(ids[1:])]
    for params, expected in [
        ({"kind": "fact"}, ids[::-1]),
        ({"review_state": "rejected"}, [ids[1]]),
        ({"provenance": "operator"}, [other]),
        ({"legacy_domain": "preferences"}, ids[::-1]),
        ({"kind": "fact", "review_state": "pending", "provenance": "extracted", "legacy_domain": "preferences"}, [ids[2], ids[0]]),
    ]:
        data = client.get(BASE, params=params).json()
        assert [r["id"] for r in data["memories"]] == expected
        assert data["total"] == len(expected)
    store.supersede(ids[2], other)
    assert client.get(BASE).json()["total"] == 3
    assert client.get(BASE, params={"include_superseded": True}).json()["total"] == 4
    assert client.get(BASE, params={"offset": 99}).json()["memories"] == []
    for params in ({"offset": -1}, {"limit": 0}, {"limit": 501}):
        assert client.get(BASE, params=params).status_code == 422


def test_patch_updates_confirms_and_preserves_rejection(review):
    client, store, _ = review
    mid = store.add("original", kind="fact", provenance="agent")
    response = client.patch(f"{BASE}/{mid}", json={"subject": "timezone", "kind": "profile", "content": "Pacific", "heading": "Zone"})
    assert response.status_code == 200
    entry = store.get(mid)
    assert (entry.subject, entry.kind, entry.content, entry.heading, entry.review_state) == ("timezone", "profile", "Pacific", "Zone", "confirmed")
    assert entry.provenance == "agent"
    assert store.search("Pacific")[0].id == mid
    for body in ({"kind": "bogus"}, {"kind": None}, {"subject": []}, {"content": " "}, {}):
        assert client.patch(f"{BASE}/{mid}", json=body).status_code == 400
    store.set_review_state(mid, "rejected")
    assert client.patch(f"{BASE}/{mid}", json={"subject": "new"}).status_code == 200
    assert store.get(mid).review_state == "rejected"
    assert client.patch(f"{BASE}/99999", json={"kind": "fact"}).status_code == 404


def test_candidates_dedupe_live_subjects_and_flag_same_kind(review):
    client, store, shared = review
    store.add("old", kind="profile", provenance="agent", subject=" Time Zone ", supersede=False)
    newest = store.add("new", kind="profile", provenance="agent", subject="time   zone", supersede=False)
    fresh = store.add("name", kind="profile", provenance="agent", subject="name")
    for kwargs in ({"review_state": "rejected"}, {"expires_at": "2000-01-01"}):
        store.add("inactive", kind="profile", provenance="agent", subject="inactive", **kwargs)
    old = store.add("superseded", kind="profile", provenance="agent", subject="retired")
    store.supersede(old, newest)
    store.add("not a candidate", kind="standing", provenance="agent", subject="rule")
    shared.add("Pacific", kind="profile", provenance="operator", subject="TIME ZONE")
    shared.add("a rule", kind="standing", provenance="operator", subject="name")
    candidates = client.get("/api/memory/profile-candidates").json()["candidates"]
    assert [(r["id"], r["already_shared"]) for r in candidates] == [(fresh, False), (newest, True)]


@pytest.mark.parametrize("kind", ["profile", "standing"])
def test_promote_once_per_subject_preserves_private(review, kind):
    client, store, shared = review
    mid = store.add("Pacific", kind=kind, provenance="agent", subject=" Time Zone ", delivery="always")
    before = asdict(store.get(mid))
    response = client.post(f"{BASE}/{mid}/promote")
    assert response.status_code == 200
    entry = shared.get(response.json()["id"])
    assert (entry.kind, entry.subject, entry.content, entry.provenance, entry.review_state, entry.delivery) == (kind, " Time Zone ", "Pacific", "operator", "confirmed", "always")
    other = store.add("Eastern", kind=kind, provenance="agent", subject="time   zone", supersede=False)
    for duplicate in (mid, other):
        response = client.post(f"{BASE}/{duplicate}/promote")
        assert response.status_code == 409 and response.json()["id"] == entry.id
    assert shared.count() == 1
    assert asdict(store.get(mid)) == before


def test_concurrent_promotions_across_store_instances(review, monkeypatch):
    client, store, shared = review
    mid = store.add("Pacific", kind="profile", provenance="agent", subject="zone")
    # Two connections to the same DB: no process-local lock can satisfy this contract.
    from operator_api.memory_routes import _promote_memory

    second = SharedProfileStore(shared.path.parent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: _promote_memory(s, store.get(mid)), [shared, second]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert next(r for r in results if not isinstance(r, dict)).status_code == 409
    assert shared.count() == 1
    fact = store.add("fact", kind="fact", provenance="agent")
    assert client.post(f"{BASE}/{fact}/promote").status_code == 400
    assert client.post(f"{BASE}/99999/promote").status_code == 404
    no_subject = store.add("unknown", kind="profile", provenance="agent")
    assert client.post(f"{BASE}/{no_subject}/promote").status_code == 400
    monkeypatch.setattr(STATE, "shared_profile_store", None)
    assert client.post(f"{BASE}/{mid}/promote").json() == {"enabled": False}
    assert client.get("/api/memory/profile-candidates").json() == {"enabled": False}


def test_hard_delete_and_disabled_store(review, monkeypatch):
    client, store, _ = review
    mid = store.add("zebra", kind="fact", provenance="agent", delivery="on_demand")
    assert client.delete(f"{BASE}/{mid}").json() == {"enabled": True, "deleted": True}
    assert store.get(mid) is None and store.search("zebra") == []
    assert client.delete(f"{BASE}/{mid}").status_code == 404
    monkeypatch.setattr(STATE, "memory_store", None)
    assert client.get(BASE).json() == {"enabled": False, "memories": [], "total": 0}
    assert client.patch(f"{BASE}/1", json={"kind": "fact"}).json() == {"enabled": False}
    assert client.delete(f"{BASE}/1").json() == {"enabled": False, "deleted": False}


@pytest.mark.parametrize("state", ["rejected", "expired", "superseded", "invalidated", "other_kind"])
def test_only_live_shared_rows_of_same_kind_block_promotion(review, state):
    client, store, shared = review
    mid = store.add("Pacific", kind="profile", provenance="agent", subject="zone")
    old = shared.add("old", kind="standing" if state == "other_kind" else "profile", provenance="agent", subject="ZONE")
    if state == "rejected":
        shared.set_review_state(old, "rejected")
    elif state == "superseded":
        replacement = shared.add("replacement", kind="profile", provenance="operator", subject="other")
        shared.supersede(old, replacement)
    elif state in {"expired", "invalidated"}:
        column = "expires_at" if state == "expired" else "invalidated_at"
        with shared._write() as db:
            db.execute(f"UPDATE memories SET {column}='2000-01-01' WHERE id=?", (old,))
    candidates = client.get("/api/memory/profile-candidates").json()["candidates"]
    assert len(candidates) == 1 and candidates[0]["already_shared"] is False
    response = client.post(f"{BASE}/{mid}/promote")
    assert response.status_code == 200
    assert shared.get(response.json()["id"]).review_state == "confirmed"


def test_pending_shared_profile_already_counts_as_live(review):
    client, store, shared = review
    mid = store.add("Pacific", kind="profile", provenance="agent", subject="zone")
    existing = shared.add("proposed", kind="profile", provenance="agent", subject="ZONE")
    assert client.get("/api/memory/profile-candidates").json()["candidates"][0]["already_shared"] is True
    response = client.post(f"{BASE}/{mid}/promote")
    assert response.status_code == 409 and response.json()["id"] == existing
    assert shared.get(existing).review_state == "pending"
