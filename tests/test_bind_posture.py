"""Bind-posture guardrail (#2147) — a single-IP bind silently excludes loopback."""

from __future__ import annotations

from a2a_impl.auth import evaluate_bind_reachability


def test_loopback_and_wildcard_binds_are_silent():
    for host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""):
        assert evaluate_bind_reachability(host) is None, host


def test_loopback_range_beyond_127_0_0_1_is_silent():
    assert evaluate_bind_reachability("127.0.0.2") is None


def test_single_interface_ip_warns_that_loopback_is_excluded():
    """The incident: network.bind set to the box's Tailscale address to scope reach,
    which stopped serving 127.0.0.1 and locked the desktop webview out entirely."""
    msg = evaluate_bind_reachability("100.101.189.45")
    assert msg and "EXCLUDES loopback" in msg
    assert "100.101.189.45" in msg
    assert "0.0.0.0" in msg  # names the correct posture instead of just refusing


def test_lan_ip_warns_too():
    assert evaluate_bind_reachability("192.168.1.50")


def test_a_hostname_bind_warns_rather_than_guessing():
    """Can't classify a hostname without resolving (which would block boot); `localhost`
    is handled explicitly, so anything else is treated as a single-host bind."""
    assert evaluate_bind_reachability("my-box.tailnet.ts.net")


def test_whitespace_is_tolerated():
    assert evaluate_bind_reachability("  127.0.0.1  ") is None
    assert evaluate_bind_reachability("  10.0.0.5  ")
