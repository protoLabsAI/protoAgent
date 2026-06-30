"""Foundational real-subprocess fleet tests (the harness skeleton).

Covers the three things the fleet had NO live coverage for: two isolated
instances, a hub spawning a real member + proxying to it, and a member resolving
its config under the new ``<ws>/config`` layout (the old double-scope footgun).

Slow + opt-in: ``PA_RUN_INTEGRATION=1 pytest tests/integration``.
"""

from __future__ import annotations

import json

from tests.integration.conftest import http_get, http_post, poll, requires_integration

pytestmark = requires_integration


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
