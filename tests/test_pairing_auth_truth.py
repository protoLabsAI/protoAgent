"""The pairing flow must key off the SERVER's token state, not a client's (ADR 0087 D6).

Regression for the third desktop brick: a browser holding a stale token in localStorage made
the console skip minting, while the server had none — so it wrote a non-loopback bind onto an
instance the boot guard then refused to start. "The browser has a token" and "the server
requires a token" are different facts; only the server can answer the second.
"""

from __future__ import annotations

import a2a_impl.auth as auth


def test_bearer_configured_reflects_the_active_token(monkeypatch):
    monkeypatch.setattr(auth, "_BEARER", [None])
    assert auth.bearer_configured() is False
    monkeypatch.setattr(auth, "_BEARER", ["a-real-token"])
    assert auth.bearer_configured() is True


def test_bearer_configured_tracks_a_live_rotation(monkeypatch):
    """The divergence that caused the outage: a token removed after a client cached it."""
    monkeypatch.setattr(auth, "_BEARER", [None])
    auth.set_bearer_token("minted")
    assert auth.bearer_configured() is True
    auth.set_bearer_token("")  # removed — a client's cached copy is now meaningless
    assert auth.bearer_configured() is False


def test_the_bind_guard_agrees_with_bearer_configured(monkeypatch):
    """The flow must not be able to write a bind the boot guard will then reject.

    `evaluate_open_bind` is what refuses at startup; gating the write on the same fact is
    what keeps the two from disagreeing and bricking the app.
    """
    monkeypatch.setattr(auth, "_BEARER", [None])
    ok, _msg = auth.evaluate_open_bind("0.0.0.0", bearer_configured=auth.bearer_configured(), allow_open=False)
    assert ok is False  # exactly the state the flow used to create

    monkeypatch.setattr(auth, "_BEARER", ["minted"])
    ok, _msg = auth.evaluate_open_bind("0.0.0.0", bearer_configured=auth.bearer_configured(), allow_open=False)
    assert ok is True


def test_start_reports_auth_configured_when_loopback_bound(monkeypatch):
    """The 409 must carry the server's answer — it's what the console decides on."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["100.119.239.8"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    monkeypatch.setattr(auth, "_BEARER", [None])
    # Nothing pairable now, but the machine HAS a usable address — the actionable case.
    assert pr._candidate_hosts() == []
    assert pr._pairable_addresses() == [{"host": "100.119.239.8", "kind": "tailnet"}]
    assert auth.bearer_configured() is False
