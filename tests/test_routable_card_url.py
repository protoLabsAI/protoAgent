"""assert_routable_card_url — deploy-time guard against a loopback agent card.

A deployed agent that advertises `http://127.0.0.1:.../a2a` (e.g. A2A_PUBLIC_URL
unset after a redeploy) is silently unreachable to remote consumers. When
`a2a.require_routable_url` is set, the agent refuses to start. Off by default so
local/desktop runs still advertise loopback correctly.
"""

from __future__ import annotations

import pytest

import server.a2a as a2a
from runtime.state import STATE


class _Cfg:
    def __init__(self, require: bool):
        self.a2a_require_routable_url = require


def _set(monkeypatch, *, require: bool, public_url: str | None, port: int = 7870):
    monkeypatch.setattr(STATE, "graph_config", _Cfg(require), raising=False)
    monkeypatch.setattr(STATE, "active_port", port, raising=False)
    if public_url is None:
        monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
    else:
        monkeypatch.setenv("A2A_PUBLIC_URL", public_url)


def test_noop_when_flag_off(monkeypatch):
    # Loopback URL, but the guard is off → must NOT exit (local/desktop default).
    _set(monkeypatch, require=False, public_url=None)
    a2a.assert_routable_card_url()  # no raise


def test_exits_on_loopback_when_required(monkeypatch):
    _set(monkeypatch, require=True, public_url=None)  # falls back to 127.0.0.1
    with pytest.raises(SystemExit) as ei:
        a2a.assert_routable_card_url()
    assert ei.value.code == 1


@pytest.mark.parametrize(
    "bad",
    [
        "http://127.0.0.1:7870",
        "http://localhost:7870",
        "http://[::1]:7870",
        "http://0.0.0.0:7870",
    ],
)
def test_exits_on_each_loopback_form(monkeypatch, bad):
    _set(monkeypatch, require=True, public_url=bad)
    with pytest.raises(SystemExit):
        a2a.assert_routable_card_url()


def test_passes_on_routable_host(monkeypatch):
    _set(monkeypatch, require=True, public_url="http://roxy:7870")
    a2a.assert_routable_card_url()  # routable → no raise
