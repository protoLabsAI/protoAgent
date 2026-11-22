"""Decision mutations require verified human credentials, including hub delegation."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from a2a_impl import auth
from graph.decisions import DecisionStore
from graph.fleet import proxy
from operator_api import decision_routes
from runtime.state import STATE
from security import devices


def _decision_app(monkeypatch):
    """A decision-routes app over an in-memory store with the parked-turn resume faked."""
    store = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", store)
    monkeypatch.setattr("graph.decisions._notify_created", lambda rec: None)
    deliveries = []

    async def resume(session_id, text, decision_id=None, *, resolution=None):
        deliveries.append(decision_id)
        if resolution:
            store.answer(decision_id, resolution["answer"], answered_by=resolution["answered_by"],
                         answered_surface=resolution["answered_surface"], answer_kind=resolution["answer_kind"])
        return True

    async def not_parked(*args):
        return False

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", resume)
    monkeypatch.setattr("graph.decision_reconcile.interrupt_parked", not_parked)
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    return store, app, deliveries


@pytest.fixture
def rig(tmp_path, monkeypatch, delegation_env):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_FLEET_MEMBER_SLUG", "alice")
    tokens = {"console": "operator-secret", "fleet": "fleet-secret", "federation": "federation-secret"}
    for name, kind in [("phone", devices.KIND_DEVICE), ("agent", devices.KIND_AGENT)]:
        code, _ = devices.start_pairing(kind)
        _, tokens[name] = devices.claim_pairing(code, name)
    store, app, deliveries = _decision_app(monkeypatch)
    auth.install(app, bearer_token=tokens["console"], fleet_token=tokens["fleet"],
                 federation_token=tokens["federation"], api_key="", allowed_origins_raw="")

    # Mint through an authenticated hub request, using the existing slice-4 helper.
    hub = FastAPI()
    hub.add_middleware(auth.A2AAuthMiddleware)

    @hub.post("/api/delegate")
    async def delegate(request: Request):
        path = (await request.json())["path"]
        return {"token": proxy.mint_member_delegation(
            "alice", request, method="POST", context=path, operation="http-request",
        )}

    with TestClient(app) as client, TestClient(hub) as hub_client:
        yield store, client, hub_client, tokens, deliveries, delegation_env


@pytest.mark.parametrize("caller", ["fleet", "agent", "federation"])
@pytest.mark.parametrize("action", ["answer", "cancel"])
def test_nonhuman_cannot_resolve_decision(rig, caller, action):
    store, client, _, tokens, deliveries, _ = rig
    rec = store.create("session", "Ship?", ["yes", "no"])
    before = store.get(rec.id).to_dict()
    response = client.post(
        f"/api/decisions/{rec.id}/{action}",
        headers={"Authorization": f"Bearer {tokens[caller]}", "X-Principal-Kind": "shared-operator-bearer",
                 "X-Human-Operator": "true"},
        json={"option_id": "yes", "answered_by": "operator", "principal_kind": "shared-operator-bearer",
              "reason": "operator said so", "message": {"answered_by": "operator"}},
    )
    assert response.status_code == 403
    assert store.get(rec.id).to_dict() == before
    assert deliveries == []


@pytest.mark.parametrize("action", ["answer", "cancel"])
def test_missing_verified_identity_fails_closed(rig, action):
    store, _, _, _, deliveries, _ = rig
    rec = store.create("session", "Ship?", ["yes", "no"])
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    with TestClient(app) as client:
        response = client.post(f"/api/decisions/{rec.id}/{action}", json={"option_id": "yes"})
    assert response.status_code == 403
    assert store.get(rec.id).pending
    assert deliveries == []


@pytest.mark.parametrize("caller", ["console", "hub", "phone", "notification"])
@pytest.mark.parametrize("action", ["answer", "cancel"])
def test_operator_can_resolve_from_each_surface_with_credential_attribution(rig, caller, action):
    store, client, hub, tokens, _, delegation = rig
    rec = store.create("session", "Ship?", ["yes", "no"])
    path = f"/api/decisions/{rec.id}/{action}"
    # A notification tap opens the console; the paired phone sends its normal credential.
    credential = "phone" if caller in {"phone", "notification"} else "console"
    headers = {"Authorization": f"Bearer {tokens[credential]}"}
    if caller == "hub":
        token = hub.post("/api/delegate", headers=headers, json={"path": path}).json()["token"]
        headers = {"Authorization": f"Bearer {tokens['fleet']}", delegation.DELEGATION_HEADER: token}
    response = client.post(path, headers=headers, json={
        "option_id": "yes", "answered_by": "forged-actor", "device_id": "forged-device",
        "message": {"answered_by": "forged-message"}, "surface": "console",
    })
    assert response.status_code == 200, response.text
    updated = store.get(rec.id)
    assert updated.state == ("answered" if action == "answer" else "withdrawn")
    if action == "answer":
        assert updated.answered_by == "operator"
        assert response.json()["decision"]["answered_by"] == "operator"
    if caller == "hub":
        # Delegations remain single-use; replay cannot answer a closed decision or join delivery.
        assert client.post(path, headers=headers, json={"option_id": "yes"}).status_code == 403


@pytest.mark.parametrize("action", ["answer", "cancel"])
def test_open_mode_operator_still_resolves(tmp_path, monkeypatch, action):
    """An instance with no credential configured (desktop loopback default) rates every
    caller the operator; the gate must not start refusing it."""
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    store, app, deliveries = _decision_app(monkeypatch)
    auth.install(app, bearer_token=None, api_key="", allowed_origins_raw="")
    assert auth.open_mode()
    rec = store.create("session", "Ship?", ["yes", "no"])
    with TestClient(app) as client:
        response = client.post(f"/api/decisions/{rec.id}/{action}",
                               json={"option_id": "yes", "answered_by": "forged-actor"})
    assert response.status_code == 200, response.text
    updated = store.get(rec.id)
    assert updated.state == ("answered" if action == "answer" else "withdrawn")
    if action == "answer":
        assert updated.answered_by == "operator"
        assert deliveries == [rec.id]


@pytest.mark.parametrize("action", ["answer", "cancel"])
def test_open_mode_without_middleware_stamp_fails_closed(tmp_path, monkeypatch, action):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    store, app, deliveries = _decision_app(monkeypatch)
    assert auth.open_mode()
    rec = store.create("session", "Ship?", ["yes", "no"])
    with TestClient(app) as client:
        response = client.post(f"/api/decisions/{rec.id}/{action}", json={"option_id": "yes"})
    assert response.status_code == 403
    assert store.get(rec.id).pending
    assert deliveries == []
