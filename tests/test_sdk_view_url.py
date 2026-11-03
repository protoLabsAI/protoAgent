"""``sdk.view_url`` — the absolute console deep link to a plugin view (QR labels, notifications).

The console half (``apps/web/src/lib/viewDeepLink.ts``) reads ``?plugin=&view=&vp=``; these pin
the server half: which base a person can actually open, and that ids/params are validated the
same way the console will validate them.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

import graph.workspaces.manager as manager
import runtime.state as rs
from graph import sdk
from security import public_console


@pytest.fixture(autouse=True)
def _server(monkeypatch):
    # A server bound on 127.0.0.1:7953 and no public URL, unless a test says otherwise.
    monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
    monkeypatch.setattr(rs.STATE, "main_loop", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "active_port", 7953, raising=False)
    monkeypatch.setattr(rs.STATE, "bind_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(manager, "is_workspace_member", lambda: False)
    public_console.set_public_console_url(None)
    yield
    public_console.set_public_console_url(None)


def _q(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def test_hub_member_links_to_the_hub_slug_window(monkeypatch):
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://hub.example.ts.net/agents/merchantAgent-6604")
    monkeypatch.setattr(manager, "is_workspace_member", lambda: True)
    url = sdk.view_url("inventory", "view", {"bin": "B07"})
    assert url == "https://hub.example.ts.net/app/agent/merchantAgent-6604/?plugin=inventory&view=view&vp=bin%3DB07"


def test_public_console_origin_when_declared():
    public_console.set_public_console_url("https://agent.example.com")
    assert sdk.view_url("inventory") == "https://agent.example.com/app/?plugin=inventory"


def test_falls_back_to_the_bound_console():
    assert sdk.view_url("boardy", "stats") == "http://127.0.0.1:7953/app/?plugin=boardy&view=stats"


def test_plain_public_url_is_the_base(monkeypatch):
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://roxy.example/")
    assert sdk.view_url("boardy").startswith("https://roxy.example/app/?plugin=boardy")


def test_no_server_is_none(monkeypatch):
    monkeypatch.setattr(rs.STATE, "main_loop", None, raising=False)
    assert sdk.view_url("boardy") is None


@pytest.mark.parametrize(
    "params, expected",
    [
        ({"bin": "B07", "from": "qr"}, "bin=B07&from=qr"),
        ([("a", "1"), ("a", "2")], "a=1&a=2"),
        ("?bin=B07", "bin=B07"),
        ({"path": "/x?y#z"}, "path=%2Fx%3Fy%23z"),
        (None, None),
    ],
)
def test_params_are_query_data(params, expected):
    q = _q(sdk.view_url("inventory", params=params))
    assert q.get("vp") == expected
    assert q["plugin"] == "inventory" and "view" not in q


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "https://evil.example", "-lead"])
def test_bad_plugin_id_raises(bad):
    with pytest.raises(ValueError):
        sdk.view_url(bad)


def test_bad_view_id_raises():
    with pytest.raises(ValueError):
        sdk.view_url("inventory", "a/b")


def test_oversized_params_raise():
    with pytest.raises(ValueError):
        sdk.view_url("inventory", params={"k": "x" * 3000})
