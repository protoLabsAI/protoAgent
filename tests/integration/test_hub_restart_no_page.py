"""A hub restart raises no "member down" page; a real member crash still does (#5648 fix 8).

The claim under check: an app update restarts the hub, the hub's teardown stops its
members, and the fleet-health loop might page the operator that a member is ``down``
(or ``wedged`` / ``gave_up``) although nothing went wrong. Those three states are the
ones that still push to a phone (importance high in ``_FLEET_HEALTH_NOTICE``).

Each case boots a real hub with ``fleet.health`` on and a 1s sweep, has it spawn a real
member, lets the sweep see the member healthy, then restarts the hub:

* graceful: SIGTERM the hub process (how the app stops it for an update), then boot a
  NEW hub process on the same instance home, data root and port;
* in place: ``POST /api/restart`` (the hub drains, then re-execs itself).

Either way the member is started again through ``POST /api/fleet/<name>/start`` and the
sweep runs several more times. There must then be no paging fleet-health inbox item for
the member, and no row at all in the hub's push ledger, with one push-enrolled device
seeded in the hub's instance root.

Positive control, in the same test and against the restarted hub: SIGKILL the member's
pid directly. A ``down`` inbox item and a high-importance push ledger row must appear
within a few sweeps. That proves the restarted hub's health loop was sweeping all along
and that both assertions above can fail.

No push service is contacted: the seeded device's client keys are not a real
subscription, so the sender ends the attempt before any request. The ledger row stays.

Slow + opt-in: ``PA_RUN_INTEGRATION=1 pytest tests/integration``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import time

import pytest

from infra.paths import reset_instance_paths
from security import devices
from tests.integration.conftest import http_get, http_post, poll, requires_integration

pytestmark = requires_integration

MEMBER = "beta"
#: The floor the health loop allows (``max(1.0, interval)``).
SWEEP_INTERVAL_S = 1
#: Consecutive failed probes of a LIVE member before it is ``wedged``. The shipped
#: default is 3 sweeps of 60s (three minutes without an answer). At a 1s sweep the same
#: count would be three seconds, shorter than a member's boot, so the count is raised
#: to keep ``wedged`` meaning a member that stays unresponsive well past its boot.
FAILURE_THRESHOLD = 60
#: How long each "nothing may be raised" window lasts: several whole sweeps.
QUIET_WINDOW_S = 8 * SWEEP_INTERVAL_S

HEALTH_CONFIG = (
    "fleet:\n"
    "  health:\n"
    "    enabled: true\n"
    f"    interval_seconds: {SWEEP_INTERVAL_S}\n"
    f"    failure_threshold: {FAILURE_THRESHOLD}\n"
    # Paging only: an automatic restart would turn the control's `down` into `restarted`.
    "    restart_dead: false\n"
)


def _member(hub, mid: str) -> dict:
    st, raw = http_get(f"{hub.base}/api/fleet", timeout=10)
    assert st == 200, raw[:200]
    return next((a for a in json.loads(raw).get("agents", []) or [] if a.get("id") == mid), {})


def _health_items(hub, mid: str) -> list[dict]:
    """Every fleet-health inbox item about this member, handled or not."""
    st, raw = http_get(f"{hub.base}/api/inbox?floor=later&include_delivered=true", timeout=10)
    assert st == 200, f"hub inbox unreadable: {st} {raw[:200]}"
    return [
        item for item in json.loads(raw)["items"]
        if item.get("source") == "fleet.health" and f"({mid})" in str(item.get("text") or "")
    ]


def _pages(hub, mid: str) -> list[str]:
    """Texts of the items that page: the member is down or wedged, or recovery gave up."""
    paging = re.compile(rf"\({re.escape(mid)}\) (is down|is wedged|recovery gave up)")
    return [item["text"] for item in _health_items(hub, mid) if paging.search(item["text"])]


def _push_attempts(hub) -> list[tuple]:
    """Every delivery attempt in the hub's push ledger (read-only; the hub owns the file)."""
    ledger = hub.home / "push" / "delivery.sqlite3"
    if not ledger.exists():
        return []
    db = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        return db.execute("SELECT notice_id, device_id, kind, importance FROM attempts").fetchall()
    except sqlite3.OperationalError:  # the hub has not created the table yet
        return []
    finally:
        db.close()


def _seed_push_device(hub, monkeypatch) -> str:
    """One push-enrolled device in the hub's instance root (the registry is read per notice)."""
    with monkeypatch.context() as patch:
        patch.setenv("PROTOAGENT_HOME", str(hub.home))
        patch.delenv("PROTOAGENT_INSTANCE", raising=False)
        reset_instance_paths()
        device, token = devices._register("phone")
        assert devices.set_push_binding(
            device.id, token, endpoint="https://web.push.apple.com/integration-test",
            p256dh="client-key", auth="secret", vapid_version=1,
        )
    reset_instance_paths()
    return device.id


def _wait_member_serving(hub, mid: str) -> None:
    assert poll(lambda: http_get(f"{hub.base}/agents/{mid}/healthz", timeout=5)[0] == 200, timeout=90), (
        "the member process never became healthy behind the hub"
    )


def _assert_quiet(hub, mid: str, when: str) -> None:
    """Let several sweeps run, then require that nothing paged and nothing was pushed."""
    time.sleep(QUIET_WINDOW_S)
    assert _member(hub, mid).get("running"), f"the member is not running {when}: {_member(hub, mid)}"
    pages = _pages(hub, mid)
    assert not pages, f"a fleet-health page was raised {when}: {pages}"
    attempts = _push_attempts(hub)
    assert not attempts, f"a notice reached the push ledger {when}: {attempts}"


def _relaunch(fleet, hub):
    """Graceful restart the way an app update does it: SIGTERM, then a new process."""
    hub.proc.send_signal(signal.SIGTERM)
    hub.proc.wait(timeout=60)
    return fleet(name=hub.name, reuse=hub)


def _boot_id(hub) -> str:
    st, raw = http_get(f"{hub.base}/api/restart/readiness", timeout=5)
    return str(json.loads(raw).get("boot_id") or "") if st == 200 else ""


def _restart_in_place(fleet, hub):
    """``POST /api/restart``: the hub drains, runs its shutdown hook, then re-execs itself."""
    before = _boot_id(hub)
    assert before, "the hub reports no boot id to tell the restart by"
    st, raw = http_post(f"{hub.base}/api/restart", {"reason": "integration test"})
    assert st == 202 and json.loads(raw).get("restarting") is True, f"restart refused: {st} {raw[:300]}"
    assert poll(lambda: _boot_id(hub) not in ("", before), timeout=180), "the hub never came back after its restart"
    assert hub.proc.poll() is None, "the hub process exited instead of restarting in place"
    return hub


@pytest.mark.parametrize("restart_hub", [_relaunch, _restart_in_place], ids=["graceful-relaunch", "in-place-restart"])
def test_hub_restart_raises_no_page_but_a_member_crash_does(fleet, monkeypatch, restart_hub):
    hub = fleet(name="hubhealth", extra_config=HEALTH_CONFIG)

    st, raw = http_post(f"{hub.base}/api/fleet", {"name": MEMBER, "inherit_config": True, "start": True}, timeout=180)
    assert st == 200, f"create member failed: {st} {raw[:300]}"
    mid = json.loads(raw)["agent"]["id"]
    _wait_member_serving(hub, mid)
    device_id = _seed_push_device(hub, monkeypatch)

    # The sweep sees the member healthy under the first hub, and that alone raises nothing.
    _assert_quiet(hub, mid, "before any restart")
    first_pid = int(_member(hub, mid)["pid"])

    hub = restart_hub(fleet, hub)

    # The hub's teardown stopped the member; nothing brings it back but a start.
    assert poll(lambda: not _member(hub, mid).get("running"), timeout=30), (
        f"the member is still listed as running after the hub restart: {_member(hub, mid)}"
    )
    st, raw = http_post(f"{hub.base}/api/fleet/{MEMBER}/start", {}, timeout=180)
    assert st == 200, f"member start after the hub restart failed: {st} {raw[:300]}"
    restarted = json.loads(raw)["agent"]
    assert restarted.get("running") and int(restarted["pid"]) != first_pid, restarted
    _wait_member_serving(hub, mid)

    # The claim: the restart, and the member coming back, page nobody.
    _assert_quiet(hub, mid, "after the hub restart")

    # Positive control: a crash nobody asked for. Killed directly, not through the hub.
    os.kill(int(restarted["pid"]), signal.SIGKILL)
    pages = poll(lambda: _pages(hub, mid), timeout=30 * SWEEP_INTERVAL_S, interval=0.5)
    assert pages and all("is down" in text for text in pages), (
        f"a SIGKILLed member raised no down page: {[item['text'] for item in _health_items(hub, mid)]}"
    )
    assert len(pages) == 1, f"one crash must be one page, got {pages}"
    # The notice seam queues the push adapter without waiting for the ledger write.
    attempts = poll(lambda: _push_attempts(hub), timeout=15, interval=0.25)
    assert attempts and len(attempts) == 1, f"the down page did not reach the push ledger exactly once: {attempts}"
    assert attempts[0][1:] == (device_id, "notify", "high"), attempts
