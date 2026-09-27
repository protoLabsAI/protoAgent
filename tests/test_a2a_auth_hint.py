"""A 401 from an A2A peer says what to do about it (#3042 live finding).

A tokenless delegate presents the fleet service token on LOOPBACK ONLY (ADR 0089) — the
token must never leave the box. So an off-box delegate sends no credential at all, by
design, and 401s. The bare passthrough ("HTTP 401: Unauthorized: expected 'Authorization:
Bearer '") tells the operator that something is unauthorized but not that the cause is a
deliberate decision they had no way to know about.
"""

from __future__ import annotations

from plugins.delegates.adapters import Delegate, _a2a_auth_hint


def _d(**kw) -> Delegate:
    d = Delegate(name=kw.pop("name", "protoEngineer"), type="a2a")
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def test_an_off_box_401_names_the_field_and_the_reason():
    hint = _a2a_auth_hint(_d(url="http://192.168.1.20:7875/a2a", auth_token=""), 401)
    assert "no Auth token" in hint
    assert "never leaves the box" in hint
    assert "federation_token" in hint  # the least-privilege option, named first


def test_a_403_gets_the_same_hint():
    assert _a2a_auth_hint(_d(url="http://peer.lan:7875/a2a", auth_token=""), 403)


def test_a_configured_token_gets_no_hint():
    """The credential was sent and rejected — a wrong token, not a missing one. Telling
    the operator to set the field they already set would send them the wrong way."""
    assert _a2a_auth_hint(_d(url="http://192.168.1.20:7875/a2a", auth_token="secret"), 401) == ""


def test_a_loopback_401_gets_a_DIFFERENT_hint():
    """Loopback should have presented the service token, so a 401 there means the lookup
    failed rather than the token being withheld. Different cause, different fix."""
    hint = _a2a_auth_hint(_d(url="http://127.0.0.1:7875/a2a", auth_token=""), 401)
    assert "loopback delegate" in hint
    assert "could not be resolved" in hint
    assert "never leaves the box" not in hint  # that explanation would be wrong here


def test_localhost_counts_as_loopback():
    assert "loopback delegate" in _a2a_auth_hint(_d(url="http://localhost:7875/a2a"), 401)


def test_a_non_auth_error_gets_no_hint():
    for code in (400, 404, 429, 500, 503):
        assert _a2a_auth_hint(_d(url="http://192.168.1.20:7875/a2a", auth_token=""), code) == ""


def test_a_hub_proxied_401_says_pair_it_if_remote(monkeypatch):
    """ADR 0113 D4: a delegate to a remote fleet member points at the hub's loopback proxy.
    The old loopback hint ("the fleet service token could not be resolved") sent the operator
    after the wrong fix. The adapter can't know whether the slug is a remote, so the hint is
    conditional rather than asserting it."""
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "FLEET")
    hint = _a2a_auth_hint(_d(name="ava", url="http://127.0.0.1:7870/agents/ava-1a2b/a2a", auth_token=""), 401)
    assert "hub proxy at http://127.0.0.1:7870/agents/ava-1a2b/a2a refused the call" in hint
    assert "If 'ava-1a2b' is a remote fleet member" in hint and "Pair" in hint
    assert "could not be resolved" not in hint


def test_a_hub_proxied_401_without_a_fleet_token_blames_the_hub_hop(monkeypatch):
    """No fleet token resolved → no credential went out, so the HUB refused, not the remote."""

    def _boom():
        raise RuntimeError("not in a fleet")

    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", _boom)
    hint = _a2a_auth_hint(_d(url="http://127.0.0.1:7870/agents/ava-1a2b/a2a", auth_token=""), 401)
    assert "no fleet service token could be resolved" in hint
    assert "the hub itself refused" in hint
    assert "Pair" not in hint


def test_the_hubs_own_host_path_is_not_a_proxied_remote():
    # /agents/host/a2a is the hub itself; a bare /a2a or a non-loopback proxy path isn't D4.
    assert "loopback delegate" in _a2a_auth_hint(_d(url="http://127.0.0.1:7870/agents/host/a2a"), 401)
    assert "loopback delegate" in _a2a_auth_hint(_d(url="http://127.0.0.1:7870/a2a"), 401)
    assert "not loopback" in _a2a_auth_hint(_d(url="http://10.0.0.5:7870/agents/ava/a2a"), 401)
