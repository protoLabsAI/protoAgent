"""The graphless chat prompt (#2458 / #4566) names the right state.

``server.chat._setup_required_message`` is what /api/chat, the OpenAI-compatible endpoint and
A2A return when ``STATE.graph is None``. There are THREE graphless states, each with its own
prompt: the setup wizard hasn't run (finish it), a native OAuth provider is signed out
(reconnect), or setup is complete with no model connected ("Explore without a model", #4566 —
connect one in Settings ▸ Model). Explore mode must NOT tell the visitor to finish a wizard
that is already complete.
"""

from __future__ import annotations


def _message(monkeypatch, *, setup_complete, auth_error=None):
    import graph.config_io as cio
    from runtime.state import STATE
    from server.chat import _setup_required_message

    monkeypatch.setattr(STATE, "graph_auth_error", auth_error, raising=False)
    # chat.py imports is_setup_complete lazily from graph.config_io at call time.
    monkeypatch.setattr(cio, "is_setup_complete", lambda: setup_complete)
    return _setup_required_message()[0]["content"]


def test_setup_pending_points_at_the_wizard(monkeypatch):
    content = _message(monkeypatch, setup_complete=False)
    assert "setup wizard" in content.lower()


def test_signed_out_points_at_reconnect(monkeypatch):
    content = _message(monkeypatch, setup_complete=True, auth_error={"message": "Reconnect the provider."})
    assert "signed out" in content.lower()
    assert "Reconnect the provider." in content


def test_explore_mode_points_at_settings_model(monkeypatch):
    """#4566: setup complete, no model, no auth error → connect-a-model, not the wizard."""
    content = _message(monkeypatch, setup_complete=True)
    assert "no model connected" in content.lower()
    assert "Settings ▸ Model" in content
    assert "wizard" not in content.lower()  # the bug: don't send a done user back to setup
