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
    assert "--host <that address>" in err
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


# ── --device: pair a PHONE from a headless box (ADR 0087 D3 amendment, #4336 S2) ──────


def _device_body(**extra) -> dict:
    return {
        "ok": True,
        "kind": "device",
        "code": "K7QM2-XPA4F",
        "expires_at": time.time() + 300,
        "ttl": 300,
        "hosts": [
            {
                "host": "100.64.1.2",
                "kind": "tailnet",
                "base_url": "http://100.64.1.2:7870/app/",
                "url": "http://100.64.1.2:7870/app/#pair=K7QM2-XPA4F",
                "qr": "<svg/>",
            },
            {
                "host": "192.168.1.20",
                "kind": "lan",
                "base_url": "http://192.168.1.20:7870/app/",
                "url": "http://192.168.1.20:7870/app/#pair=K7QM2-XPA4F",
                "qr": "<svg/>",
            },
        ],
        **extra,
    }


def test_pair_device_asks_for_a_device_code_with_the_same_credential(monkeypatch, capsys):
    got: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        got["auth"] = request.headers.get("Authorization")
        got["body"] = json.loads(request.content)
        return httpx.Response(200, json=_device_body())

    _connect_to(monkeypatch, handler)
    assert pair.run_pair_cli(["--device"]) == 0
    assert got == {"auth": "Bearer fleet-tok", "body": {"kind": "device"}}
    out = capsys.readouterr().out
    assert "fleet-tok" not in out


def test_pair_device_prints_code_urls_and_a_terminal_qr(monkeypatch, capsys):
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=_device_body()))
    assert pair.run_pair_cli(["--device"]) == 0
    out = capsys.readouterr().out
    assert "Pairing code:  K7QM2-XPA4F" in out
    # The typed path's URLs are the BASE console URLs (no fragment), tailnet first.
    assert out.index("http://100.64.1.2:7870/app/") < out.index("http://192.168.1.20:7870/app/")
    assert "Have a pairing code?" in out
    assert "Scan with the phone's camera" in out
    assert "█" in out or "▀" in out  # the half-block QR
    # Not the agent flow's hub instructions.
    assert "fleet pair" not in out


def test_pair_device_qr_encodes_the_best_link(monkeypatch, capsys):
    seen: list = []
    monkeypatch.setattr(pair, "_terminal_qr", lambda url, light: seen.append((url, light)) or "QR\n")
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=_device_body()))
    assert pair.run_pair_cli(["--device", "--light"]) == 0
    assert seen == [("http://100.64.1.2:7870/app/#pair=K7QM2-XPA4F", True)]


def test_pair_device_no_qr(monkeypatch, capsys):
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=_device_body()))
    assert pair.run_pair_cli(["--device", "--no-qr"]) == 0
    out = capsys.readouterr().out
    assert "Scan with" not in out and "K7QM2-XPA4F" in out


def test_pair_device_falls_back_to_the_link_minus_fragment(monkeypatch, capsys):
    """An instance from before `base_url` still yields an openable console URL — never the
    `#pair=` link printed as 'open this and type the code'."""
    body = _device_body(hosts=[{"host": "192.168.1.20", "kind": "lan", "url": "http://192.168.1.20:7870/app/#pair=x"}])
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=body))
    assert pair.run_pair_cli(["--device", "--no-qr"]) == 0
    out = capsys.readouterr().out
    assert "  http://192.168.1.20:7870/app/" in out and "#pair=" not in out


def test_pair_device_on_a_loopback_instance_explains_the_fix(monkeypatch, capsys):
    body = {"ok": False, "error": "This agent only listens on localhost, so a phone can't reach it.", "available": []}
    _connect_to(monkeypatch, lambda r: httpx.Response(409, json=body))
    assert pair.run_pair_cli(["--device"]) == 1
    err = capsys.readouterr().err
    assert "a phone can't reach it" in err
    assert "`protoagent pair --device` again" in err
    assert "ALLOW_OPEN" not in err


def test_pair_device_json(monkeypatch, capsys):
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=_device_body()))
    assert pair.run_pair_cli(["--device", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "device"


def test_terminal_qr_renders_a_scannable_block_drawing():
    drawn = pair._terminal_qr("http://100.64.1.2:7870/app/#pair=K7QM2-XPA4F", light=False)
    assert drawn and len(drawn.splitlines()) > 10


def test_pair_device_on_a_ui_none_instance_says_to_run_the_console(monkeypatch, capsys):
    """`--ui none` serves no console for the phone to open: say so, exit non-zero, and skip
    the bind advice (the instance may be perfectly reachable)."""
    from operator_api.pairing_routes import NO_CONSOLE_ERROR

    body = {"ok": False, "reason": "no_console", "error": NO_CONSOLE_ERROR, "hosts": [], "available": []}
    _connect_to(monkeypatch, lambda r: httpx.Response(409, json=body))
    assert pair.run_pair_cli(["--device"]) == 1
    captured = capsys.readouterr()
    assert "--ui console" in captured.err
    assert "--host" not in captured.err
    assert "Pairing code" not in captured.out


def test_pair_on_a_loopback_instance_with_tailscale_points_at_serve(monkeypatch, capsys):
    """#4336 S4: on Tailscale, the localhost-friendly fix (tailscale serve) is named first."""
    body = {
        "ok": False,
        "error": "This agent only listens on localhost, so a phone can't reach it.",
        "available": [{"host": "100.64.1.2", "kind": "tailnet"}],
        "bind": "127.0.0.1",
        "auth_configured": True,
        "reach": {"tailscale": {"running": True, "dns_name": "box.tail1234.ts.net"}},
    }
    _connect_to(monkeypatch, lambda r: httpx.Response(409, json=body))
    assert pair.run_pair_cli(["--device"]) == 1
    err = capsys.readouterr().err
    assert "box.tail1234.ts.net" in err and "HTTPS via Tailscale" in err


def test_pair_device_qr_uses_the_best_address_and_says_what_each_needs(monkeypatch, capsys):
    """The QR encodes hosts[0] — the tailnet HTTPS name when served — and every address says
    plainly that a tailnet one needs Tailscale on the phone."""
    body = {
        "ok": True,
        "kind": "device",
        "code": "3HV8T-QW6RN",
        "expires_at": 9999999999,
        "ttl": 300,
        "hosts": [
            {
                "host": "box.tail1234.ts.net",
                "kind": "tailnet",
                "via": "serve",
                "secure": True,
                "base_url": "https://box.tail1234.ts.net/app/",
                "url": "https://box.tail1234.ts.net/app/#pair=3HV8T-QW6RN",
            },
            {
                "host": "192.168.1.20",
                "kind": "lan",
                "via": "ip",
                "base_url": "http://192.168.1.20:7870/app/",
                "url": "http://192.168.1.20:7870/app/#pair=3HV8T-QW6RN",
            },
        ],
    }
    seen = []
    monkeypatch.setattr(pair, "_terminal_qr", lambda url, light: seen.append(url) or "QR\n")
    _connect_to(monkeypatch, lambda r: httpx.Response(200, json=body))
    assert pair.run_pair_cli(["--device"]) == 0
    out = capsys.readouterr().out
    assert seen == ["https://box.tail1234.ts.net/app/#pair=3HV8T-QW6RN"]
    assert "https://box.tail1234.ts.net/app/   (HTTPS from any network; the phone needs the Tailscale app" in out
    assert "http://192.168.1.20:7870/app/   (same Wi-Fi only)" in out
