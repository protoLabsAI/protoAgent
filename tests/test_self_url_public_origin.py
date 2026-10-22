"""``show_config``'s ``self_url`` public origin follows the canonical console-base
precedence (#4446 item 3) — ``A2A_PUBLIC_URL`` first, then the configured pairing
origin — the same derivation the A2A card and ``infra.media`` / ``graph.sdk`` use
(#4256), so an env-only deployed agent reports the public URL it actually serves on."""

from __future__ import annotations

import json

import pytest

from graph.config import LangGraphConfig
from infra import media
from tools.config_tools import build_config_tools


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No ambient public URL and no server serving, so each test pins only what it
    asserts on — monkeypatch restores both after the test."""
    monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "main_loop", None, raising=False)


def _self_url(cfg):
    tool = build_config_tools(cfg)[0]
    return json.loads(tool.invoke({"section": "self_url"}))["self_url"]


def test_configured_public_url_still_reported():
    """The pre-existing case: ``public_console_url`` set, no env — unchanged."""
    cfg = LangGraphConfig()
    cfg.public_console_url = "https://agent.example.com"
    out = _self_url(cfg)

    assert out["public"] == "https://agent.example.com"
    assert out["preferred"] == "https://agent.example.com"


def test_env_only_public_url_matches_canonical_helpers(monkeypatch):
    """r1: A2A_PUBLIC_URL-only configuration yields the same public/preferred origin as
    the canonical helpers — a deployed agent that sets the env (the common case) but has
    no configured pairing origin must report its public URL, not fall back to local."""
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://roxy.example")
    out = _self_url(LangGraphConfig())

    assert out["public"] == "https://roxy.example"
    assert out["preferred"] == "https://roxy.example"
    assert out["public"] == media.console_base_url() == media.configured_public_url()


def test_env_wins_over_config(monkeypatch):
    """r2: the env-versus-config precedence agrees with the canonical helpers —
    A2A_PUBLIC_URL (env) takes the same top priority it has in
    ``infra.media.console_base_url`` (``configured_public_url() or bound_base_url``)."""
    cfg = LangGraphConfig()
    cfg.public_console_url = "https://configured.example.com"
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://env.example.com")
    out = _self_url(cfg)

    assert out["public"] == "https://env.example.com"
    assert out["preferred"] == "https://env.example.com"


def test_env_normalised_like_the_canonical_helper(monkeypatch):
    """Whitespace + trailing slash are trimmed exactly as the card/``/media`` links do,
    because this reuses ``infra.media.configured_public_url`` rather than re-deriving."""
    monkeypatch.setenv("A2A_PUBLIC_URL", "  https://roxy.example/  ")
    out = _self_url(LangGraphConfig())

    assert out["public"] == "https://roxy.example"
    assert out["preferred"] == "https://roxy.example"


def test_blank_env_falls_back_to_config(monkeypatch):
    """A whitespace-only A2A_PUBLIC_URL is unset, so the configured origin still shows."""
    cfg = LangGraphConfig()
    cfg.public_console_url = "https://agent.example.com"
    monkeypatch.setenv("A2A_PUBLIC_URL", "   ")
    out = _self_url(cfg)

    assert out["public"] == "https://agent.example.com"
    assert out["preferred"] == "https://agent.example.com"


def test_neither_set_preferred_is_local(monkeypatch):
    """Neither env nor config set: no public origin, so ``preferred`` is the bound local
    address — the local bind semantics are unchanged."""
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "main_loop", object(), raising=False)
    monkeypatch.setattr(STATE, "bind_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(STATE, "active_port", 7871, raising=False)
    out = _self_url(LangGraphConfig())

    assert out["public"] == ""
    assert out["local"] == "http://127.0.0.1:7871"
    assert out["preferred"] == "http://127.0.0.1:7871"
    assert "note" not in out


def test_unknown_local_bind_keeps_the_note(monkeypatch):
    """No server serving: local is "" and the unknown-port note is unchanged; a public
    origin (env) is still reported and preferred even with no local server."""
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://roxy.example")
    out = _self_url(LangGraphConfig())

    assert out["local"] == ""
    assert out["note"]
    assert out["public"] == "https://roxy.example"
    assert out["preferred"] == "https://roxy.example"


def test_env_public_url_never_probes(monkeypatch):
    """The env-only path must not open a socket either — the point of self_url holds for
    the new derivation too."""
    import socket

    def _no_sockets(*a, **k):
        raise AssertionError("show_config(self_url) must not open a socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://roxy.example")
    out = _self_url(LangGraphConfig())

    assert out["public"] == "https://roxy.example"
