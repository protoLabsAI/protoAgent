"""Only a member's question (or urgent) notice reaches the hub's push ledger (#5648).

A real hub boots with its shipped push policy (``push.all_notices`` off, no quiet hours)
and spawns a real member. Three member notices then arrive at the hub:

* a routine one the MEMBER PROCESS raises itself: a watch set through the member's own
  ``POST /api/watches`` trips on the member's watch loop, which raises a (notify, normal)
  notice and hands it to the hub over loopback (``graph.fleet.notice_relay``);
* a (review, normal) and a (question, normal) handoff posted to the hub's
  ``/api/fleet/notices`` with that member's workspace id and the fleet service token.

The member's own notice must also be on the hub's event stream (``GET /api/events``),
which is where the desktop app's native banner reads notices: the hub raises a relayed
notice through its own notice seam, not only into the push ledger.

The hub records who raised each one (the ``origins`` table), which proves all three were
taken. Only the question may have rows in the ``attempts`` table.

No push service is contacted: the seeded device's client keys are not a real
subscription, so the sender ends the attempt before any request. The ledger row stays.

Slow + opt-in: ``PA_RUN_INTEGRATION=1 pytest tests/integration``.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request

from infra.paths import reset_instance_paths
from security import devices
from tests.integration.conftest import http_get, http_post, poll, requires_integration

pytestmark = requires_integration

FLEET_TOKEN = "fleet-token-for-the-push-policy-test"


def _rows(path, query, *params):
    with sqlite3.connect(path) as db:
        return db.execute(query, params).fetchall()


def _replayed_notices(base: str, *, until, timeout: float = 30.0) -> dict[str, dict]:
    """The ``notice`` events the hub replays from the start of its stream, by notice id.

    Connects the way the desktop app does (apps/desktop/src-tauri/src/notices.rs):
    ``Last-Event-ID: 0``. The stream never ends, so reading stops once ``until(notices)``
    is true or the timeout passes.
    """
    notices: dict[str, dict] = {}
    request = urllib.request.Request(
        f"{base}/api/events", headers={"Accept": "text/event-stream", "Last-Event-ID": "0"}
    )
    deadline = time.time() + timeout
    try:
        # A keepalive comment arrives every 15s, so a read never blocks past the socket timeout.
        with urllib.request.urlopen(request, timeout=20) as stream:
            while time.time() < deadline and not until(notices):
                line = stream.readline().decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                frame = json.loads(line[len("data:"):])
                if frame.get("topic") == "notice":
                    notices[frame["data"]["notice_id"]] = frame["data"]
    except OSError:
        pass  # a timed-out read: the caller asserts on what was replayed
    return notices


def test_only_a_real_members_question_reaches_the_hubs_push_ledger(fleet, monkeypatch):
    # The hub hands this to the member it spawns, so the test knows the member's credential.
    hub = fleet(name="hubpolicy", extra_env={"PROTOAGENT_FLEET_TOKEN": FLEET_TOKEN})
    ledger = hub.home / "push" / "delivery.sqlite3"

    st, raw = http_post(f"{hub.base}/api/fleet", {"name": "beta", "inherit_config": True, "start": True}, timeout=180)
    assert st == 200, f"create member failed: {st} {raw[:300]}"
    member = json.loads(raw)["agent"]
    proxied = f"{hub.base}/agents/{member['id']}"
    assert poll(lambda: http_get(f"{proxied}/healthz", timeout=5)[0] == 200, timeout=90), (
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

    # 1. The member raises a routine notice of its own: a watch whose check passes at once.
    #    Its watch loop first ticks ~15s after boot, then every 30s.
    st, raw = http_post(
        f"{proxied}/api/watches",
        {"condition": "the push policy check passes", "verifier": {"type": "command", "command": "exit 0"}},
    )
    assert st == 200 and json.loads(raw).get("ok") is True, f"member refused the watch: {st} {raw[:300]}"
    raised = poll(
        lambda: _rows(ledger, "SELECT notice_id FROM origins WHERE agent = ?", member["id"]), timeout=120, interval=2
    )
    assert raised, "the member's own watch notice never reached the hub"
    watch_notice = raised[0][0]

    # The hub raised it through its own notice seam, so it is on the hub's event stream,
    # where the desktop app's native banner reads it (the push ledger alone is not enough).
    replayed = _replayed_notices(hub.base, until=lambda notices: watch_notice in notices)
    assert watch_notice in replayed, f"the member's notice is not on the hub's event stream: {sorted(replayed)}"
    event = replayed[watch_notice]
    assert (event["kind"], event["importance"], event["link"]) == ("notify", "normal", "")
    # The hub's own wording: the member's text never crosses.
    assert event["text"].startswith("A notice from ") and "push policy check" not in event["text"]

    # 2. A routine result and a question, handed over the way the member's relay does.
    stamp = time.time()
    for notice_id, kind in (("review-1", "review"), ("question-1", "question")):
        st, raw = http_post(
            f"{hub.base}/api/fleet/notices",
            {"member": member["id"], "notice_id": notice_id, "kind": kind, "importance": "normal",
             "created_at": stamp, "expires_at": stamp + 3600},
            headers={"Authorization": f"Bearer {FLEET_TOKEN}"},
        )
        # 2xx for the one kept off push too: the member must not retry it.
        assert st == 200 and json.loads(raw) == {"ok": True, "queued": True}, (notice_id, st, raw[:300])

    # The hub took all three, each recorded against the member that raised it.
    origins = {row[0] for row in _rows(ledger, "SELECT notice_id FROM origins WHERE agent = ?", member["id"])}
    assert origins == {watch_notice, "review-1", "question-1"}, origins
    # Only the question has delivery attempts, one for the one enrolled device.
    # The hub's notice seam queues the push adapter and answers the handoff without
    # waiting for the ledger write, so the row is polled for.
    poll(lambda: _rows(ledger, "SELECT 1 FROM attempts"), timeout=15, interval=0.25)
    attempts = _rows(ledger, "SELECT notice_id, device_id, kind, importance FROM attempts")
    assert attempts == [("question-1", device.id, "question", "normal")], (
        f"only the question may reach the push ledger, got {attempts}"
    )
