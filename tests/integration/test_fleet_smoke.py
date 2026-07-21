"""Foundational real-subprocess fleet tests (the harness skeleton).

Covers the three things the fleet had NO live coverage for: two isolated
instances, a hub spawning a real member + proxying to it, and a member resolving
its config under the new ``<ws>/config`` layout (the old double-scope footgun).

Slow + opt-in: ``PA_RUN_INTEGRATION=1 pytest tests/integration``.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.integration.conftest import http_get, http_post, poll, requires_integration

pytestmark = requires_integration

# tests/integration/test_fleet_smoke.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_two_instances_boot_isolated(fleet):
    a = fleet(name="inst-a")
    b = fleet(name="inst-b")

    for s in (a, b):
        st, raw = http_get(f"{s.base}/.well-known/agent-card.json")
        assert st == 200, (s.name, st, raw[:200])
        assert json.loads(raw).get("name"), f"{s.name} agent card has no name"

    # Disjoint roots — config (new layout) under each PROTOAGENT_HOME, stores under each tmp HOME.
    assert a.home != b.home
    assert a.data_root != b.data_root
    assert (a.home / "config" / "langgraph-config.yaml").exists()
    assert (b.home / "config" / "langgraph-config.yaml").exists()
    # Each instance keeps its own data root (no cross-talk).
    assert not (b.data_root / ".protoagent").is_relative_to(a.data_root)


def test_hub_spawns_member_and_proxies(fleet):
    hub = fleet(name="hub")

    st, raw = http_post(f"{hub.base}/api/fleet", {"name": "alpha", "inherit_config": True, "start": True}, timeout=180)
    assert st == 200, f"create member failed: {st} {raw[:300]}"
    agent = json.loads(raw)["agent"]
    assert agent.get("running"), f"member did not start: {agent}"
    mid = agent["id"]

    # The hub's fleet registry lists the member (agents = [host, ...members, ...remotes]).
    st, raw = http_get(f"{hub.base}/api/fleet")
    assert st == 200, raw[:200]
    ids = [a.get("id") for a in json.loads(raw).get("agents", [])]
    assert mid in ids, f"member {mid} not in fleet {ids}"

    # Proxy round-trip: the hub forwards /agents/<id>/healthz to the real member process.
    reached = poll(lambda: http_get(f"{hub.base}/agents/{mid}/healthz", timeout=3)[0] == 200, timeout=90)
    assert reached, "member never reachable through the hub proxy"

    # The member serves its OWN agent card through the proxy.
    st, raw = http_get(f"{hub.base}/agents/{mid}/.well-known/agent-card.json", timeout=10)
    assert st == 200, f"proxied agent card: {st} {raw[:200]}"
    assert json.loads(raw).get("name"), "proxied member card has no name"


def test_member_config_resolves_under_new_layout(fleet):
    """A member is launched with PROTOAGENT_HOME=<ws>; its config must land at
    <ws>/config/ (NOT double-scoped under <ws>/<id>/), and the inherited gateway
    must read back — the exact regression the old _config_scope/_reset bug caused."""
    hub = fleet(name="hubcfg")

    st, raw = http_post(f"{hub.base}/api/fleet", {"name": "beta", "inherit_config": True, "start": True}, timeout=180)
    assert st == 200, f"create member failed: {st} {raw[:300]}"
    mid = json.loads(raw)["agent"]["id"]

    # Workspaces are HUB-instance-scoped now (``instance_root/workspaces`` =
    # ``PROTOAGENT_HOME/workspaces`` = under hub.home), so the member's config lives at
    # <hub.home>/workspaces/<ws>/config/langgraph-config.yaml.
    matches = list(hub.home.glob(f"workspaces/{mid}/config/langgraph-config.yaml"))
    assert matches, (
        "member config not at <ws>/config/ (new layout). langgraph-config.yaml files under hub home: "
        f"{[str(p) for p in hub.home.rglob('langgraph-config.yaml')]}"
    )
    # It carries the inherited fake gateway — i.e. config wrote + read back at the un-double-scoped path.
    assert "127.0.0.1" in matches[0].read_text(), "member config did not inherit the host gateway"

    # And the member serves that config back through the proxy (the running process reads the same path).
    cfg = poll(
        lambda: (lambda r: json.loads(r[1]) if r[0] == 200 else None)(
            http_get(f"{hub.base}/agents/{mid}/api/config", timeout=5)
        ),
        timeout=90,
    )
    assert cfg is not None, "member /api/config not reachable through the proxy"
    assert "127.0.0.1" in json.dumps(cfg), f"member config api missing inherited gateway: {json.dumps(cfg)[:300]}"


def _http_status_only(url: str, timeout: float = 6.0) -> int:
    """Return just the HTTP status of ``url`` without consuming the body — works for the SSE
    stream (a normal read would block until timeout). Raw socket so we read only the status line."""
    import socket
    import urllib.parse

    u = urllib.parse.urlsplit(url)
    s = socket.create_connection((u.hostname, u.port), timeout=timeout)
    try:
        path = u.path + (f"?{u.query}" if u.query else "")
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nConnection: close\r\n\r\n".encode())
        s.settimeout(timeout)
        line = s.recv(128).split(b"\r\n", 1)[0].decode("latin1")
        return int(line.split()[1])
    finally:
        s.close()


def test_hub_proxies_authed_sse_to_a_fleet_token_member(fleet):
    """ADR 0089 regression repro (roxy portfolio 'Could not load — Unauthorized').

    A member closed with the FLEET token (inherit_config:false → no auth.token → D5) has a
    bearer the hub-signed SSE token can't verify. The hub must swap the fleet token in for a
    proxied /agents/<id>/api/events, else the member 401s every live stream. Also checks a
    regular authed API call is proxied+swapped (the already-working P1/P2 path)."""
    hub = fleet(name="hub", auth_token="hub-operator-secret")
    H = {"Authorization": "Bearer hub-operator-secret"}

    st, raw = http_post(
        f"{hub.base}/api/fleet", {"name": "team", "inherit_config": True, "start": True}, timeout=180, headers=H
    )
    assert st == 200, f"create member: {st} {raw[:300]}"
    agent = json.loads(raw)["agent"]
    mid, mport = agent["id"], agent.get("port")
    assert poll(
        lambda: http_get(f"{hub.base}/agents/{mid}/healthz", timeout=3, headers=H)[0] == 200, timeout=90
    ), "unreachable"

    # Prove the member is genuinely CLOSED with the fleet token (else an SSE 200 would be a false
    # pass from an open member): a DIRECT unauthenticated call to its own port is rejected.
    assert mport, f"no member port in {agent}"
    st_direct, _ = http_get(f"http://127.0.0.1:{mport}/api/flags", timeout=5)
    assert st_direct == 401, f"member must be CLOSED (fleet token); direct unauthenticated /api/flags got {st_direct}"

    # A sister agent's public DS/static assets ride the proxy as /agents/<slug>/_ds/* and are
    # loaded by bearer-less browser requests (a plugin view's import()/<link>). A token-gated hub
    # MUST serve them anonymously — the member already does — or the DS plugin-kit 401s and the
    # plugin view drops to unauthenticated fetch, whose data call then 401s (the "Could not load"
    # class, roxy portfolio). This is the fix; the member serves /_ds/ regardless of auth.
    #
    # Assert on AUTH, not on the asset's existence. The contract under test is "the hub does not
    # gate this path"; whether `plugin-kit.js` is on disk depends on whether the console was built
    # (`apps/web/dist/`), which the CI fleet job does NOT do — it installs Python deps only. Pinning
    # 200 therefore passed locally (dist present) and failed every CI run from the commit that added
    # it, reporting a proxy-auth regression that wasn't one. A 404 here is the member honestly saying
    # "no such file" — it still proves the request was proxied anonymously rather than 401'd.
    st_ds = http_get(f"{hub.base}/agents/{mid}/_ds/plugin-kit.js", timeout=10)[0]
    assert st_ds not in (401, 403), (
        f"closed hub must serve a sister's /_ds/ assets WITHOUT a bearer, got {st_ds}"
    )
    if (REPO_ROOT / "apps" / "web" / "dist" / "_ds" / "plugin-kit.js").exists():
        # Console built → the asset really should come back, so keep the strong check where it means
        # something (local runs, and any job that builds the SPA first).
        assert st_ds == 200, f"built console present but the proxied DS asset returned {st_ds}"

    # Regular authed API call through the proxy → 200 (the swap that already worked).
    st, _ = http_get(f"{hub.base}/agents/{mid}/api/flags", timeout=10, headers=H)
    assert st == 200, f"proxied authed API to a closed member must be 200, got {st}"

    # SSE token minted by the HUB (signed with the hub bearer), streamed THROUGH the proxy to
    # the fleet-token member. This is the exact request that 401'd on roxy before 0.105.1.
    st, raw = http_get(f"{hub.base}/api/sse-token", timeout=10, headers=H)
    assert st == 200 and json.loads(raw).get("token"), f"sse-token mint: {st} {raw[:200]}"
    sse = json.loads(raw)["token"]
    code = _http_status_only(f"{hub.base}/agents/{mid}/api/events?token={sse}")
    assert code == 200, f"proxied SSE to a fleet-token member must be 200 (was 401 pre-fix), got {code}"
