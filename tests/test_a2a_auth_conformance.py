"""The agent card and the auth guard must agree — proven on the wire, not by inspection.

`/.well-known/agent-card.json` is a contract with strangers. A standards-driven A2A client
reads `securityRequirements`, picks one, and sends it; if the server doesn't accept exactly
that, a healthy agent looks offline. #2620 was that failure: the card advertised `X-API-Key`
on a bearer-only agent, so a conforming client got 401 from a correctly-configured server.

Shape assertions alone wouldn't have caught it — the card was internally consistent and
looked fine. What was missing is a test that reads the card the way a client does and then
*uses* it against the real middleware. That is what this file does, over every configuration:

  for each advertised requirement:   sending exactly it must be accepted
  for each credential NOT advertised: sending it must be refused

The second half matters as much as the first: a card that under-promises is safe, but a card
that omits a credential the server silently accepts is a different kind of untruth.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BEARER = "bearer-token-value"
API_KEY = "api-key-value"

# (bearer, api_key) — every combination the guard can be configured into.
CONFIGURATIONS = [
    pytest.param(BEARER, "", id="bearer-only"),
    pytest.param(None, API_KEY, id="api-key-only"),
    pytest.param(BEARER, API_KEY, id="both"),
    pytest.param(None, "", id="open-mode"),
]


def _serve(bearer, api_key):
    """A real app: the true card endpoint behind the true middleware."""
    import protolabs_a2a as pa

    import server.a2a as sa
    from a2a_impl import auth

    app = FastAPI()

    @app.get("/a2a/probe")
    async def probe():  # a guarded path — not on the public allowlist
        return {"ok": True}

    auth.install(app, bearer_token=bearer, api_key=api_key, allowed_origins_raw="")

    card = pa.build_agent_card(
        name="conformance",
        description="d",
        url="http://testserver/a2a",
        version="1",
        skills=[],
        bearer=auth.bearer_configured(),
    )
    sa._apply_real_security(card, pa)

    @app.get("/.well-known/agent-card.json")
    async def served_card():
        return {
            "securitySchemes": {k: {"_": True} for k in card.security_schemes},
            "securityRequirements": [{"schemes": dict.fromkeys(r.schemes, {})} for r in card.security_requirements],
        }

    return TestClient(app), card


def _credential_headers(scheme: str, bearer, api_key) -> dict:
    return {"Authorization": f"Bearer {bearer}"} if scheme == "bearer" else {"x-api-key": api_key}


@pytest.mark.parametrize("bearer,api_key", CONFIGURATIONS)
def test_every_advertised_requirement_is_accepted(bearer, api_key):
    """A client that follows the card must get in. This is the exact path #2620 broke."""
    client, _ = _serve(bearer, api_key)

    published = client.get("/.well-known/agent-card.json").json()
    requirements = published["securityRequirements"]

    if not requirements:  # open mode advertises nothing — and requires nothing
        assert client.get("/a2a/probe").status_code == 200
        return

    for requirement in requirements:
        headers = {}
        for scheme in requirement["schemes"]:
            headers.update(_credential_headers(scheme, bearer, api_key))
        assert client.get("/a2a/probe", headers=headers).status_code == 200, (
            f"the card advertises {sorted(requirement['schemes'])} but the server refused it"
        )


@pytest.mark.parametrize("bearer,api_key", CONFIGURATIONS)
def test_no_unadvertised_credential_is_accepted(bearer, api_key):
    """The other direction: a credential the card does NOT offer must not work. A card that
    hides a working credential is as untrue as one that invents a broken one."""
    client, card = _serve(bearer, api_key)
    advertised = set(card.security_schemes)

    if "bearer" not in advertised and bearer:
        assert client.get("/a2a/probe", headers={"Authorization": f"Bearer {bearer}"}).status_code == 401
    if "apiKey" not in advertised and api_key:
        assert client.get("/a2a/probe", headers={"x-api-key": api_key}).status_code == 401


@pytest.mark.parametrize("bearer,api_key", CONFIGURATIONS)
def test_a_wrong_credential_is_always_refused(bearer, api_key):
    """The floor: none of this may loosen the guard. Anything gated stays gated."""
    client, _ = _serve(bearer, api_key)
    gated = bool(bearer or api_key)

    wrong = {"Authorization": "Bearer wrong-value", "x-api-key": "wrong-value"}
    assert client.get("/a2a/probe", headers=wrong).status_code == (401 if gated else 200)
    assert client.get("/a2a/probe").status_code == (401 if gated else 200)


def test_the_card_is_rebuilt_from_the_guard_after_a_live_rotation():
    """`set_bearer_token` rotates the credential without re-registering routes. A card
    derived from env would keep advertising the old posture; one derived from the guard
    follows. This is the drift that produced #2620, in its live form."""
    import protolabs_a2a as pa

    import server.a2a as sa
    from a2a_impl import auth

    auth.configure(bearer_token="initial", api_key="", allowed_origins_raw="")

    def current_schemes():
        card = pa.build_agent_card(
            name="x", description="d", url="http://h/a2a", version="1", skills=[],
            bearer=auth.bearer_configured(),
        )
        sa._apply_real_security(card, pa)
        return sorted(card.security_schemes)

    assert current_schemes() == ["bearer"]

    auth.set_bearer_token("")  # operator removed the token at runtime
    assert current_schemes() == [], "the card still claimed a credential the server dropped"


# ── deprecation must warn, not break (#2632) ────────────────────────────────


def test_the_legacy_key_still_authenticates_while_deprecated(caplog):
    """Deprecated is not removed. Dropping a credential someone deployed against would
    lock them out of their own agent — the warning is the migration path, not the removal."""
    import logging

    from a2a_impl import auth

    auth._API_KEY_DEPRECATION_LOGGED[0] = False
    with caplog.at_level(logging.WARNING, logger="a2a_impl.auth"):
        client, _ = _serve(BEARER, API_KEY)

    assert client.get("/a2a/probe", headers={"x-api-key": API_KEY}).status_code == 200
    assert any("DEPRECATED" in r.message for r in caplog.records), "an unannounced deprecation is a trap"
    warning = next(r.message for r in caplog.records if "DEPRECATED" in r.message)
    assert "auth.token" in warning and "2632" in warning, "say what to migrate TO, and where it's tracked"


def test_the_deprecation_warning_fires_once_per_process(caplog):
    """`configure()` runs on every config reload. A warning that reappears every few
    minutes is one operators learn to filter out."""
    import logging

    from a2a_impl import auth

    auth._API_KEY_DEPRECATION_LOGGED[0] = False
    with caplog.at_level(logging.WARNING, logger="a2a_impl.auth"):
        for _ in range(3):
            auth.configure(bearer_token=BEARER, api_key=API_KEY, allowed_origins_raw="")

    assert sum("DEPRECATED" in r.message for r in caplog.records) == 1


def test_no_deprecation_noise_for_agents_not_using_it(caplog):
    import logging

    from a2a_impl import auth

    auth._API_KEY_DEPRECATION_LOGGED[0] = False
    with caplog.at_level(logging.WARNING, logger="a2a_impl.auth"):
        auth.configure(bearer_token=BEARER, api_key="", allowed_origins_raw="")

    assert not any("DEPRECATED" in r.message for r in caplog.records)


def test_a_deprecated_credential_is_still_advertised_while_it_works():
    """The card must keep describing the server (#2620). Hiding a credential that still
    authenticates would trade one untruth for another — the card follows enforcement, and
    stops advertising it on the day it stops working."""
    import protolabs_a2a as pa

    import server.a2a as sa
    from a2a_impl import auth

    auth.configure(bearer_token=None, api_key=API_KEY, allowed_origins_raw="")
    card = pa.build_agent_card(
        name="x", description="d", url="http://h/a2a", version="1", skills=[],
        bearer=auth.bearer_configured(),
    )
    sa._apply_real_security(card, pa)

    assert sorted(card.security_schemes) == ["apiKey"]


# ── self-invocation must survive boot order and rotation ───────────────────


def test_self_invokers_resolve_credentials_after_the_guard_is_seeded():
    """The blocker a reviewer caught in this PR.

    `_init_langgraph_agent()` (server/__init__.py:423) builds the scheduler and background
    manager ~500 lines BEFORE `auth.install()` (:928). Reading the guard at construction
    therefore yields ('', '') and every scheduled or background self-invocation 401s on a
    token-gated agent — silently, until a job fires. Resolving per request is what makes
    the order irrelevant."""
    from a2a_impl import auth
    from background.manager import BackgroundManager

    auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")  # guard not yet seeded
    mgr = BackgroundManager.__new__(BackgroundManager)
    mgr._bearer, mgr._api_key = "", ""  # what a build-time read captured

    auth.configure(bearer_token=BEARER, api_key="", allowed_origins_raw="")  # install() runs later

    assert mgr._auth_headers() == {"Authorization": f"Bearer {BEARER}"}


def test_self_invokers_follow_a_live_token_rotation():
    """Same shape as #2582 one layer down: a credential captured once goes stale the
    moment it rotates."""
    from a2a_impl import auth
    from scheduler.local import LocalScheduler

    sched = LocalScheduler.__new__(LocalScheduler)
    sched._bearer, sched._api_key = "stale-from-boot", ""
    auth.configure(bearer_token="rotated", api_key="", allowed_origins_raw="")

    assert sched._auth_headers()["Authorization"] == "Bearer rotated"


def test_explicitly_passed_credentials_still_work_without_a_guard():
    """An embedding caller that supplies credentials directly keeps working — the guard is
    a source, not a requirement."""
    from a2a_impl import auth
    from background.manager import BackgroundManager

    auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    mgr = BackgroundManager.__new__(BackgroundManager)
    mgr._bearer, mgr._api_key = "explicit", "explicit-key"

    assert mgr._auth_headers() == {"Authorization": "Bearer explicit", "X-API-Key": "explicit-key"}
