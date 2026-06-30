"""Real-subprocess fleet CRASH -> DETECT -> RESTART coverage.

Boots a hub, has it spawn a real member, hard-kills the member with SIGKILL
(no graceful shutdown), then asserts the hub detects it down PASSIVELY (a plain
``/api/fleet`` poll reports it not-running) and restarts it to a fresh, healthy
process with a NEW pid and its data dir intact.

This is the failure mode the harness had no coverage for: every prior fleet test
only exercised the happy spawn/proxy path, never a member dying underneath the hub.

A SIGKILLed member is the hub's child and lingers as a zombie until reaped, and
``os.kill(pid, 0)`` reports a zombie as alive — which used to mask the crash from
``status()`` and make ``supervisor.start`` no-op on the dead pid. ``_alive`` now
reaps the zombie (targeted ``waitpid``) before probing, so passive detection works
and a restart spawns a genuinely new process — no operator ``stop`` reconcile
needed. (See ``graph/fleet/supervisor._reap``.)

Slow + opt-in: ``PA_RUN_INTEGRATION=1 pytest tests/integration``.
"""

from __future__ import annotations

import json
import os
import signal

from tests.integration.conftest import http_get, http_post, poll, requires_integration

pytestmark = requires_integration


def _fleet_agents(hub) -> list[dict]:
    st, raw = http_get(f"{hub.base}/api/fleet", timeout=10)
    assert st == 200, raw[:200]
    return json.loads(raw).get("agents", []) or []


def _member(hub, mid: str) -> dict | None:
    return next((a for a in _fleet_agents(hub) if a.get("id") == mid), None)


def test_member_crash_detected_then_restart(fleet):
    hub = fleet(name="hub-crash")

    # Create + start a real member process.
    st, raw = http_post(
        f"{hub.base}/api/fleet",
        {"name": "victim", "inherit_config": True, "start": True},
        timeout=180,
    )
    assert st == 200, f"create member failed: {st} {raw[:300]}"
    agent = json.loads(raw)["agent"]
    assert agent.get("running"), f"member did not start: {agent}"
    mid = agent["id"]
    pid1 = int(agent["pid"])

    # It answers through the hub proxy before we crash it.
    reached = poll(lambda: http_get(f"{hub.base}/agents/{mid}/healthz", timeout=3)[0] == 200, timeout=90)
    assert reached, "member never reachable through the hub proxy before crash"

    # Fleet status agrees it's up.
    m = _member(hub, mid)
    assert m and m.get("running") and int(m.get("pid")) == pid1, f"member not listed as running: {m}"

    # Where the member's data lives -- we confirm it survives the restart.
    ws_dirs = list(hub.data_root.glob(f"**/workspaces/{mid}"))
    assert ws_dirs, "member workspace dir not found before crash"
    ws_dir = ws_dirs[0]

    # CRASH: hard-kill the member (SIGKILL = no shutdown hook, like a real crash).
    os.kill(pid1, signal.SIGKILL)

    # DETECT (passive): a plain GET /api/fleet must report the member down. The crashed
    # member is the hub's child and lingers as a zombie, but status() -> _alive() reaps
    # it (targeted waitpid) so the dead pid is seen as gone -- no operator "stop" needed.
    dead = poll(lambda: (_member(hub, mid) or {}).get("running") is False, timeout=30)
    assert dead, f"hub never marked the crashed member dead (zombie not reaped?): {_member(hub, mid)}"

    # And the proxy can no longer reach it either (its port is dead).
    down = poll(lambda: http_get(f"{hub.base}/agents/{mid}/healthz", timeout=3)[0] != 200, timeout=30)
    assert down, "hub proxy still reached the member after it was killed"

    # RESTART by NAME (the /api/fleet/{name}/start route resolves id-or-name -> supervisor.start).
    # With the zombie reaped, start() does NOT short-circuit on the dead pid -- it spawns fresh.
    st, raw = http_post(f"{hub.base}/api/fleet/victim/start", {}, timeout=180)
    assert st == 200, f"restart failed: {st} {raw[:300]}"
    restarted = json.loads(raw)["agent"]
    assert restarted.get("running"), f"member did not restart: {restarted}"
    pid2 = int(restarted["pid"])
    assert pid2 != pid1, f"restart reused the crashed pid ({pid1})"

    # HEALTHY AGAIN: the new process answers through the proxy.
    reached2 = poll(lambda: http_get(f"{hub.base}/agents/{mid}/healthz", timeout=3)[0] == 200, timeout=90)
    assert reached2, "member never reachable through the hub proxy after restart"

    # Fleet status agrees: running again, with the NEW pid.
    back = poll(
        lambda: (lambda a: bool(a) and a.get("running") and a.get("pid") == pid2)(_member(hub, mid)),
        timeout=30,
    )
    assert back, f"fleet status did not show the restarted member with the new pid: {_member(hub, mid)}"

    # The member's data dir persisted across the crash + restart.
    assert ws_dir.exists(), "member workspace dir did not survive the restart"
