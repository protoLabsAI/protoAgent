"""The hub's quiet hours hold a member's routine notice (real hub + real member, #5648).

A real hub boots with quiet hours covering the current time and ``push.all_notices`` on
(so the push policy is not what keeps a notice off the ledger), and spawns a real member.
Two member handoffs are posted to the hub's ``/api/fleet/notices`` with that member's
workspace id and the fleet service token: a routine one, which the hub must hold, and a
question, which must reach the push ledger.

No push service is contacted: the seeded device's client keys are not a real
subscription, so the sender ends the attempt before any request. The ledger row stays.

Slow + opt-in: ``PA_RUN_INTEGRATION=1 pytest tests/integration``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from infra.paths import reset_instance_paths
from security import devices
from tests.integration.conftest import http_get, http_post, poll, requires_integration

pytestmark = requires_integration

FLEET_TOKEN = "fleet-token-for-the-quiet-hours-test"


def _column(path, query):
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute(query)]


def test_hub_quiet_hours_hold_a_real_members_routine_notice(fleet, monkeypatch):
    now = datetime.now(timezone.utc)
    start, end = ((now + timedelta(hours=h)).strftime("%H:%M") for h in (-2, 2))
    hub = fleet(
        name="hubquiet",
        extra_config=(
            f'quiet_hours:\n  enabled: true\n  start: "{start}"\n  end: "{end}"\n  timezone: UTC\n'
            "push:\n  all_notices: true\n"
        ),
        # The hub hands this to the member it spawns, so the test knows the member's credential.
        extra_env={"PROTOAGENT_FLEET_TOKEN": FLEET_TOKEN},
    )

    st, raw = http_post(f"{hub.base}/api/fleet", {"name": "beta", "inherit_config": True, "start": True}, timeout=180)
    assert st == 200, f"create member failed: {st} {raw[:300]}"
    member = json.loads(raw)["agent"]
    assert poll(lambda: http_get(f"{hub.base}/agents/{member['id']}/healthz", timeout=5)[0] == 200, timeout=90), (
        "the member process never became healthy behind the hub"
    )

    # One push-enrolled device in the hub's instance root (the registry is read per request).
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

    stamp = time.time()
    for notice_id, kind in (("routine-1", "notify"), ("question-1", "question")):
        st, raw = http_post(
            f"{hub.base}/api/fleet/notices",
            {"member": member["id"], "notice_id": notice_id, "kind": kind, "importance": "normal",
             "created_at": stamp, "expires_at": stamp + 3600},
            headers={"Authorization": f"Bearer {FLEET_TOKEN}"},
        )
        # 2xx for the held one too: the member must not retry it.
        assert st == 200 and json.loads(raw) == {"ok": True, "queued": True}, (notice_id, st, raw[:300])

    ledger = _column(hub.home / "push" / "delivery.sqlite3", "SELECT DISTINCT notice_id FROM attempts")
    assert ledger == ["question-1"], f"only the question may reach the push ledger, got {ledger}"
    held = _column(hub.home / "notices" / "hold.sqlite3", "SELECT text FROM held")
    assert held == [f"A notice from {member['name']}"], held
