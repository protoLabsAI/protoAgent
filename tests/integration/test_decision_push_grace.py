"""Real hub decision push grace (#5669), with an enrolled phone and no push service.

These opt-in tests take seconds because they observe the real 8 s grace, 6 s
presence expiry, and 15 s push drain across a server process boundary.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from infra.paths import reset_instance_paths
from security import devices
from tests.integration.conftest import http_get, http_post, poll, requires_integration
from tests.integration.test_attended_decision_no_push import _DecidingModel, _MARK

pytestmark = [requires_integration, pytest.mark.platform_sensitive]

_GRACE = 8
_STALE = 6
_DRAIN = 15
_SLACK = 5
_TOKEN = "integration-grace-operator"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture(scope="module")
def fake_gateway():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DecidingModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def hub(fleet, monkeypatch):
    hub = fleet(name="hubgrace", auth_token=_TOKEN, extra_env={
        "PA_DECISION_PUSH_GRACE_S": str(_GRACE),
        "PA_PRESENCE_STALE_S": str(_STALE),
    })
    try:
        with monkeypatch.context() as patch:
            patch.setenv("PROTOAGENT_HOME", str(hub.home))
            patch.delenv("PROTOAGENT_INSTANCE", raising=False)
            reset_instance_paths()
            device, token = devices._register("phone")
            # Invalid client keys fail locally, before any push service is contacted.
            assert devices.set_push_binding(
                device.id, token, endpoint="https://web.push.apple.com/test",
                p256dh="client-key", auth="secret", vapid_version=1,
            )
    finally:
        reset_instance_paths()
    return hub


def _presence(hub, session):
    status, raw = http_post(f"{hub.base}/api/chat/presence", {
        "console_id": "desktop", "session": session, "present": True, "input_age_s": 1,
    }, headers=_HEADERS)
    assert status == 204, raw
    return time.time()


def _decision(hub, session):
    message = {"role": "ROLE_USER", "parts": [{"text": _MARK}],
               "messageId": f"m-{session}", "contextId": session}
    started = time.time()
    status, raw = http_post(f"{hub.base}/a2a", {
        "jsonrpc": "2.0", "id": session, "method": "SendStreamingMessage",
        "params": {"message": message},
    }, timeout=120, headers={**_HEADERS, "A2A-Version": "1.0"})
    assert status == 200, raw[:600]

    def recorded():
        status, body = http_get(
            f"{hub.base}/api/decisions?state=pending&session_id={session}", headers=_HEADERS,
        )
        assert status == 200, body
        return json.loads(body).get("decisions")

    records = poll(recorded, timeout=10, interval=0.1)
    assert records and len(records) == 1, raw[:600]
    return records[0]["id"], started


def _row(hub, decision):
    ledger = hub.home / "push" / "delivery.sqlite3"
    if not ledger.exists():
        return None
    with sqlite3.connect(ledger) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT state, attempts, next_at, created_at FROM attempts WHERE notice_id = ?",
            (f"decision-{decision}",),
        ).fetchall()
    assert len(rows) <= 1, "expected exactly one enrolled recipient"
    return dict(rows[0]) if rows else None


def _delayed(hub, decision):
    row = poll(lambda: _row(hub, decision), timeout=2, interval=0.05)
    assert row and row["state"] == "pending" and row["attempts"] == 0, row
    assert row["next_at"] - row["created_at"] >= _GRACE, row
    assert time.time() < row["next_at"], "must observe the row before grace expires"
    return row["next_at"]


def _wait_until(deadline):
    assert poll(lambda: time.time() >= deadline,
                timeout=max(0, deadline - time.time()) + _SLACK, interval=0.1)


def _attempted(hub, decision, deadline):
    row = poll(
        lambda: (r := _row(hub, decision)) and (r["attempts"] or r["state"] == "obsolete") and r,
        timeout=max(0, deadline - time.time()), interval=0.1,
    )
    assert row and row["attempts"] >= 1 and row["state"] != "obsolete", row


def test_no_presence_pushes_at_once(hub):
    """Josh's phone buzzes at once without presence, matching #5669: delay, never drop; fail open."""
    decision, started = _decision(hub, "no-presence")
    _attempted(hub, decision, started + _DRAIN + _SLACK)


def test_answer_at_desk_prevents_buzz(hub):
    """Josh's phone stays silent because he answered at the desk, matching #5669: delay, never drop; fail open."""
    _presence(hub, "answered-at-desk")
    decision, _ = _decision(hub, "answered-at-desk")
    due = _delayed(hub, decision)
    status, raw = http_post(f"{hub.base}/api/decisions/{decision}/answer",
                            {"text": "Ship it", "surface": "console"}, headers=_HEADERS)
    assert status == 200, raw
    status, raw = http_get(f"{hub.base}/api/decisions/{decision}", headers=_HEADERS)
    assert status == 200 and json.loads(raw)["state"] == "answered", raw
    assert time.time() < due, "the answer must land before the grace ends"
    _wait_until(due + _DRAIN)
    row = _row(hub, decision)
    assert row and row["state"] == "obsolete" and row["attempts"] == 0, row


def test_unanswered_presence_delays_but_never_drops(hub):
    """Josh's phone buzzes about a grace later if unanswered, matching #5669: delay, never drop; fail open."""
    _presence(hub, "unanswered-at-desk")
    decision, _ = _decision(hub, "unanswered-at-desk")
    due = _delayed(hub, decision)
    violations = []

    def grace_elapsed():
        if time.time() >= due:
            return True
        row = _row(hub, decision)
        if not row or row["attempts"] != 0 or row["state"] != "pending":
            violations.append(row)
            return True
        return False

    assert poll(grace_elapsed, timeout=_GRACE + _SLACK, interval=0.05)
    assert not violations, f"push changed before grace ended: {violations}"
    _attempted(hub, decision, due + _DRAIN + _SLACK)


def test_presence_on_different_chat_pushes_at_once(hub):
    """Josh's phone buzzes at once for a different chat, matching #5669: delay, never drop; fail open."""
    _presence(hub, "other-chat")
    decision, started = _decision(hub, "different-chat")
    _attempted(hub, decision, started + _DRAIN + _SLACK)


def test_stale_presence_pushes_at_once(hub):
    """Josh's phone buzzes at once after presence goes stale, matching #5669: delay, never drop; fail open."""
    reported = _presence(hub, "stale-chat")
    _wait_until(reported + _STALE + 0.25)
    decision, started = _decision(hub, "stale-chat")
    _attempted(hub, decision, started + _DRAIN + _SLACK)
