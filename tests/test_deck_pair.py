"""``protoagent pair`` (ADR 0113 D7): the remote half of agent pairing for headless boxes.

The command asks the RUNNING local instance for an agent code over its operator API and
prints what a hub needs to claim it. The instance is reached through the fleet deck's hub
client, so these tests fake the wire at httpx and the discovery at ``deck.hub.connect``.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from deck import hub as deckhub
from deck import pair

URL = "http://127.0.0.1:7870"


def _client(handler, token: str | None = "fleet-tok") -> deckhub.HubClient:
    return deckhub.HubClient(URL, token, transport=httpx.MockTransport(handler))


def _connect_to(monkeypatch, handler, token: str | None = "fleet-tok"):
    seen: dict = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return deckhub.Connection(
            client=_client(handler, token),
            candidate=deckhub.HubCandidate(URL, "heartbeat"),
            card={"name": "ava"},
        )

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    return seen


def _ok_body(**extra) -> dict:
    return {
        "ok": True,
        "kind": "agent",
        "name": "ava",
        "code": "K7QM2-XPA4F",
        "expires_at": time.time() + 300,
        "ttl": 300,
        "hosts": [
            {"host": "100.64.1.2", "kind": "tailnet", "url": "http://100.64.1.2:7870"},
            {"host": "192.168.1.20", "kind": "lan", "url": "http://192.168.1.20:7870"},
        ],
        **extra,
    }


def test_pairing_start_asks_for_an_agent_code_with_the_credential():
    got: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        got["path"] = request.url.path
        got["auth"] = request.headers.get("Authorization")
        got["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    with _client(handler) as c:
        res = c.pairing_start()
    assert got == {"path": "/api/pairing/start", "auth": "Bearer fleet-tok", "body": {"kind": "agent"}}
    assert res["code"] == "K7QM2-XPA4F"


def test_pairing_start_returns_the_loopback_409_instead_of_raising():
    """The 409 is the instance saying what it COULD bind — the operator needs that body."""
    body = {"ok": False, "error": "only listens on localhost", "available": [{"host": "100.64.1.2", "kind": "tailnet"}]}

    with _client(lambda r: httpx.Response(409, json=body)) as c:
        assert c.pairing_start() == body


def test_pairing_start_still_raises_on_a_rejected_credential():
    with _client(lambda r: httpx.Response(401, json={"detail": "nope"})) as c, pytest.raises(deckhub.HubUnauthorized):
        c.pairing_start()


def test_pair_prints_the_code_and_a_claim_command_per_address(monkeypatch, capsys):
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=_ok_body()))
    assert pair.run_pair_cli([]) == 0
    out = capsys.readouterr().out
    assert "K7QM2-XPA4F" in out
    # tailnet first, as the instance ordered them — the safer address leads
    assert out.index("http://100.64.1.2:7870") < out.index("http://192.168.1.20:7870")
    assert "protoagent fleet pair http://100.64.1.2:7870 K7QM2-XPA4F" in out
    assert "fleet-tok" not in out  # the credential that minted the code is never printed


def test_pair_builds_a_url_from_the_reached_port_when_a_host_has_none(monkeypatch, capsys):
    body = _ok_body(hosts=[{"host": "192.168.1.20", "kind": "lan"}])
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=body))
    assert pair.run_pair_cli([]) == 0
    assert "protoagent fleet pair http://192.168.1.20:7870 K7QM2-XPA4F" in capsys.readouterr().out


def test_pair_on_a_loopback_instance_explains_the_fix_and_never_suggests_open_mode(monkeypatch, capsys):
    body = {
        "ok": False,
        "error": "This agent only listens on localhost, so a phone can't reach it.",
        "available": [{"host": "100.64.1.2", "kind": "tailnet"}],
        "bind": "127.0.0.1",
        "auth_configured": False,
    }
    _connect_to(monkeypatch, lambda r: httpx.Response(409, json=body))
    assert pair.run_pair_cli([]) == 1
    err = capsys.readouterr().err
    assert "100.64.1.2 (tailnet)" in err
    assert "--host 0.0.0.0" in err
    assert "no auth token" in err
    # ADR 0087 D6: the fix is a reachable bind WITH a token, never an open instance.
    assert "ALLOW_OPEN" not in err


def test_pair_json_mode_emits_the_raw_answer(monkeypatch, capsys):
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=_ok_body()))
    assert pair.run_pair_cli(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["code"] == "K7QM2-XPA4F"


def test_pair_passes_url_and_token_through_to_the_hub_search(monkeypatch):
    seen = _connect_to(monkeypatch, lambda r: httpx.Response(200, json=_ok_body()))
    pair.run_pair_cli(["--url", "http://127.0.0.1:7999", "--token", "t"])
    assert seen["url"] == "http://127.0.0.1:7999" and seen["token"] == "t"


def test_pair_against_a_member_says_to_pair_the_hub(monkeypatch, capsys):
    def fake_connect(**kwargs):
        raise deckhub.NoHub([URL], [], members=[URL])

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    assert pair.run_pair_cli([]) == 1
    assert "Pair the hub instead" in capsys.readouterr().err


def test_pair_with_nothing_running_reports_it(monkeypatch, capsys):
    def fake_connect(**kwargs):
        raise deckhub.NoHub([URL], [])

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    assert pair.run_pair_cli([]) == 1
    assert "no hub answered" in capsys.readouterr().err


def test_pair_is_a_protoagent_subcommand():
    from server import cli

    assert cli._FORWARD["pair"] == ("deck.pair", "run_pair_cli")
    assert "pair" in cli._FORWARD_HELP


def test_pair_refuses_a_phone_code_from_an_instance_that_predates_agent_pairing(monkeypatch, capsys):
    """Seen live: a pre-0113 server ignores `kind` and returns a phone code whose url is a
    `#pair=` console link. Printing that as a `fleet pair` target would never work."""
    phone = {
        "ok": True,
        "code": "EULO7xFnwvv4EupKwYwBSwYuxJKMJWXM",
        "expires_at": time.time() + 120,
        "hosts": [{"host": "192.168.1.20", "kind": "lan", "url": "http://192.168.1.20:7870/app/#pair=EULO7"}],
    }
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=phone))
    assert pair.run_pair_cli([]) == 1
    captured = capsys.readouterr()
    assert "predates agent pairing" in captured.err
    assert "#pair=" not in captured.out + captured.err
