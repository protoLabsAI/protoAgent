"""``graph.sdk.console_url()`` — the URL this agent's console is served on (#4256).

Plugins that drive their own console (Atlas's recorder, Campaign's shot scripts) used to guess
``http://localhost:7870`` — on a multi-agent machine, another agent. The seam shares the A2A
card's / absolute ``/media`` links' derivation (``infra.media``): ``A2A_PUBLIC_URL``, else the
bound host/port of the running server, else ``None``.
"""

from __future__ import annotations

import pytest

import graph.workspaces.manager as manager
import runtime.state as rs
from graph import sdk
from infra import media


@pytest.fixture(autouse=True)
def _server(monkeypatch):
    """A running server on :7953 bound to loopback, no public URL, not a fleet member."""
    monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
    monkeypatch.setattr(rs.STATE, "main_loop", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "active_port", 7953, raising=False)
    monkeypatch.setattr(rs.STATE, "bind_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(manager, "is_workspace_member", lambda: False)


def test_a2a_public_url_wins_normalised(monkeypatch):
    monkeypatch.setenv("A2A_PUBLIC_URL", "  https://roxy.example/  ")
    assert sdk.console_url() == "https://roxy.example"


def test_a2a_public_url_wins_even_with_no_server(monkeypatch):
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://roxy.example")
    monkeypatch.setattr(rs.STATE, "main_loop", None, raising=False)
    assert sdk.console_url() == "https://roxy.example"


def test_blank_public_url_falls_back_to_the_bound_port(monkeypatch):
    monkeypatch.setenv("A2A_PUBLIC_URL", "   ")
    assert sdk.console_url() == "http://127.0.0.1:7953"


@pytest.mark.parametrize(
    ("bind", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:7953"),
        ("localhost", "http://127.0.0.1:7953"),
        ("LOCALHOST", "http://127.0.0.1:7953"),
        ("localhost.", "http://127.0.0.1:7953"),
        ("::1", "http://[::1]:7953"),
        ("[::1]", "http://[::1]:7953"),
        ("127.0.0.2", "http://127.0.0.2:7953"),
        ("0.0.0.0", "http://127.0.0.1:7953"),
        ("::", "http://127.0.0.1:7953"),
        ("", "http://127.0.0.1:7953"),
        ("192.168.1.20", "http://192.168.1.20:7953"),
        ("fd00::5", "http://[fd00::5]:7953"),
        ("roxy.lan", "http://roxy.lan:7953"),
    ],
)
def test_bind_variants(monkeypatch, bind, expected):
    monkeypatch.setattr(rs.STATE, "bind_host", bind, raising=False)
    assert sdk.console_url() == expected


def test_no_server_running_is_none(monkeypatch):
    # active_port's class default is 7870 before anything binds — never trusted on its own.
    monkeypatch.setattr(rs.STATE, "main_loop", None, raising=False)
    monkeypatch.setattr(rs.STATE, "active_port", 7870, raising=False)
    assert sdk.console_url() is None


def test_no_port_is_none(monkeypatch):
    monkeypatch.setattr(rs.STATE, "active_port", None, raising=False)
    assert sdk.console_url() is None


def test_one_derivation_with_the_card_and_media_links(monkeypatch):
    from server.a2a import _a2a_card_url

    monkeypatch.setenv("A2A_PUBLIC_URL", "https://roxy.example/")
    assert sdk.console_url() == media.public_base_url() == "https://roxy.example"
    assert _a2a_card_url() == f"{sdk.console_url()}/a2a"


def test_hub_spawned_member_returns_its_own_console(monkeypatch):
    # The supervisor injects {hub}/agents/<id> so the member's CARD is dial-able through the
    # hub — not a console base. The member's own bound console is what a recorder needs.
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://hub.example/agents/ws-ab12")
    monkeypatch.setattr(manager, "is_workspace_member", lambda: True)
    assert sdk.console_url() == "http://127.0.0.1:7953"
    # ... while the card keeps advertising the hub tenant path.
    assert media.public_base_url() == "https://hub.example/agents/ws-ab12"


def test_hub_spawned_member_with_no_server_is_none(monkeypatch):
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://hub.example/agents/ws-ab12")
    monkeypatch.setattr(manager, "is_workspace_member", lambda: True)
    monkeypatch.setattr(rs.STATE, "main_loop", None, raising=False)
    assert sdk.console_url() is None


def test_member_with_an_explicit_public_url_keeps_it(monkeypatch):
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://custom.example/base")
    monkeypatch.setattr(manager, "is_workspace_member", lambda: True)
    assert sdk.console_url() == "https://custom.example/base"


def test_tenant_shaped_url_on_a_non_member_is_kept(monkeypatch):
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://proxy.example/agents/roxy")
    assert sdk.console_url() == "https://proxy.example/agents/roxy"
