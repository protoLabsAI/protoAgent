"""Device identity polish (#4336 S3, ADR 0087): rename, the active window, name hygiene.

Kept apart from ``test_device_pairing.py`` (the claim/throttle/address contract) so the two
evolve without stepping on each other.
"""

from __future__ import annotations

import importlib
import json
import time

import pytest

# Imports security.devices, whose registry I/O branches on os.name (Windows share retries).
pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def devices(tmp_path, monkeypatch):
    """A `security.devices` bound to a throwaway instance root."""
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-identity")

    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


def _pair(devices, name="iPhone"):
    code, _ = devices.start_pairing()
    device, token = devices.claim_pairing(code, name)
    return device, token


# ── rename ─────────────────────────────────────────────────────────────────────────────


def test_rename_changes_only_the_name(devices):
    device, token = _pair(devices)
    renamed = devices.rename_device(device["id"], "  Josh's   iPhone ")
    assert renamed["name"] == "Josh's iPhone"  # trimmed, whitespace runs collapsed
    assert renamed["id"] == device["id"] and renamed["created_at"] == device["created_at"]
    # Identity only — the token keeps working, so renaming never signs the device out.
    assert devices.verify_token(token).name == "Josh's iPhone"
    assert [d["name"] for d in devices.list_devices()] == ["Josh's iPhone"]


def test_rename_persists_across_a_reload(devices):
    device, _ = _pair(devices)
    devices.rename_device(device["id"], "Kitchen iPad")
    raw = json.loads(devices._registry_path().read_text("utf-8"))
    assert raw[0]["name"] == "Kitchen iPad"


def test_rename_unknown_device_is_none(devices):
    assert devices.rename_device("nope", "anything") is None


def test_rename_accepts_unicode_names(devices):
    device, _ = _pair(devices)
    assert devices.rename_device(device["id"], "Jösh’s 手机 📱")["name"] == "Jösh’s 手机 📱"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "x" * 65,
        "evil‮gnp.exe",  # bidi override — a name that renders as something else
        "two\nlines",
        "nul\x00byte",
        "zero​width",
        None,
        42,
        ["list"],
    ],
)
def test_rename_refuses_unusable_names(devices, bad):
    device, _ = _pair(devices)
    with pytest.raises(ValueError):
        devices.rename_device(device["id"], bad)
    assert devices.list_devices()[0]["name"] == "iPhone"  # untouched


def test_sixty_four_chars_is_the_limit(devices):
    device, _ = _pair(devices)
    assert devices.rename_device(device["id"], "y" * 64)["name"] == "y" * 64


def test_claim_names_are_cleaned_not_refused(devices):
    """A claim name is an unauthenticated UA sniff: repaired, never a reason to fail pairing."""
    device, _ = _pair(devices, "Phone‮\x00  with\tjunk" + "z" * 80)
    assert "‮" not in device["name"] and "\x00" not in device["name"]
    assert device["name"].startswith("Phone with junk")
    assert len(device["name"]) <= devices.MAX_NAME_LEN
    blank, _ = _pair(devices, "‮​")
    assert blank["name"] == "Unnamed device"


# ── active window ─────────────────────────────────────────────────────────────────────


def test_active_window_covers_the_last_seen_throttle(devices):
    """The badge used to need a stamp < 60 s old while the stamp was refreshed every 300 s.
    A device in continuous use must read active for the whole throttle window."""
    assert devices.ACTIVE_WINDOW_SECONDS > devices._LAST_SEEN_THROTTLE_SECONDS
    now = 1_000_000.0
    assert devices.is_active(now - devices._LAST_SEEN_THROTTLE_SECONDS + 1, now=now)
    assert devices.is_active(now - devices._LAST_SEEN_THROTTLE_SECONDS - 30, now=now)
    assert not devices.is_active(now - devices.ACTIVE_WINDOW_SECONDS - 1, now=now)
    assert not devices.is_active(None, now=now)


def test_public_shape_carries_active(devices):
    device, token = _pair(devices)
    assert device["active"] is False  # never used yet
    devices.verify_token(token)  # first use stamps last_seen
    assert devices.list_devices()[0]["active"] is True
    assert "token_sha256" not in devices.list_devices()[0]


def test_a_stale_stamp_reads_inactive(devices, monkeypatch):
    device, token = _pair(devices)
    devices.verify_token(token)
    later = time.time() + devices.ACTIVE_WINDOW_SECONDS + 5
    monkeypatch.setattr(devices.time, "time", lambda: later)
    assert devices.list_devices()[0]["active"] is False


# ── route ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(devices, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.pairing_routes as pr

    monkeypatch.delenv("PROTOAGENT_UI", raising=False)
    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    return TestClient(app)


def test_patch_route_renames(client, devices):
    device, _ = _pair(devices)
    r = client.patch(f"/api/devices/{device['id']}", json={"name": "Work phone"})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["device"]["name"] == "Work phone"
    assert client.get("/api/devices").json()["devices"][0]["name"] == "Work phone"


def test_patch_route_400s_a_bad_name_with_a_reason(client, devices):
    device, _ = _pair(devices)
    r = client.patch(f"/api/devices/{device['id']}", json={"name": "x" * 200})
    assert r.status_code == 400 and "64" in r.json()["error"]
    r = client.patch(f"/api/devices/{device['id']}", json={})
    assert r.status_code == 400
    r = client.patch(f"/api/devices/{device['id']}", content=b"not json")
    assert r.status_code == 400


def test_patch_route_404s_an_unknown_device(client):
    r = client.patch("/api/devices/missing", json={"name": "ok"})
    assert r.status_code == 404


def test_rename_route_is_operator_authenticated():
    """Only the claim is on the allowlist — the device routes, PATCH included, need the bearer."""
    from a2a_impl.auth import _is_public

    assert _is_public("/api/devices/abc123") is False
    assert _is_public("/api/devices") is False
