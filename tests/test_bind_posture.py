"""Bind-posture guardrail (#2147) — a single-IP bind silently excludes loopback."""

from __future__ import annotations

from a2a_impl.auth import evaluate_bind_reachability, evaluate_public_url


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


# ── A2A_PUBLIC_URL guardrail (#4252) ─────────────────────────────────────────────────


def test_loopback_bind_without_public_url_is_silent():
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2", ""):
        assert evaluate_public_url(host, None) is None, host


def test_wide_or_interface_bind_without_public_url_warns():
    for host in ("0.0.0.0", "::", "192.168.1.50", "roxy"):
        msg = evaluate_public_url(host, "")
        assert msg and "A2A_PUBLIC_URL" in msg and "loopback" in msg, host


def test_public_url_set_silences_the_warning():
    assert evaluate_public_url("0.0.0.0", "https://agent.example.com") is None
    assert evaluate_public_url("0.0.0.0", "   ") is not None  # whitespace isn't a URL


def test_server_boot_logs_the_public_url_warning():
    # The boot path calls it with the RESOLVED bind + the env var, and logs a warning.
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "server" / "__init__.py").read_text()
    assert 'auth.evaluate_public_url(args.host, os.environ.get("A2A_PUBLIC_URL"))' in src


def _status_warnings(monkeypatch, bind: str, extra: list[str]) -> list[str]:
    import asyncio

    from operator_api import console_handlers as ch
    from operator_api import pairing_routes as pr

    monkeypatch.setattr(pr, "_BIND_HOST", [bind])
    monkeypatch.setattr(pr, "_EXTRA_LISTEN", list(extra))
    status = asyncio.run(ch._operator_runtime_status())
    return status.get("warnings") or []


def test_runtime_status_is_silent_when_a_picked_ip_also_serves_loopback(monkeypatch):
    # #4336 S4: a tailnet-IP bind listens on 127.0.0.1 too, so the #2147 banner is false.
    warnings = _status_warnings(monkeypatch, "100.119.239.8", ["127.0.0.1"])
    assert not any("EXCLUDES loopback" in w for w in warnings)


def test_runtime_status_warns_when_the_loopback_listener_did_not_come_up(monkeypatch):
    warnings = _status_warnings(monkeypatch, "100.119.239.8", [])
    assert any("EXCLUDES loopback" in w for w in warnings)
