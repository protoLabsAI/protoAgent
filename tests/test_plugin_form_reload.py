"""Pending plugin form callbacks survive a plugin reload (#2889).

``reload_plugins`` can re-import ``graph.slash_commands``; the pending-form dict
used to be a plain module global, so every form opened before the reload could
only ever answer "This form has expired." The dict now lives in
``graph._form_state``, anchored in ``sys.modules`` under a synthetic key the
import machinery never re-executes — a re-import of either module hands back the
same dict, so open→reload→submit round-trips.
"""

from __future__ import annotations

import importlib
import sys


async def _open_form(monkeypatch, session_id: str):
    """Register a form-opening chat command and open its form; returns the request."""
    from graph import slash_commands as sc
    from runtime.state import STATE

    async def on_submit(answers, session_id):
        return f"done:{answers.get('name')}"

    async def handler(rest, session_id):
        return {"form": {"kind": "form", "title": "Name", "steps": [{"schema": {}}]}, "on_submit": on_submit}

    monkeypatch.setattr(STATE, "plugin_chat_commands", {"c": handler})
    req = await sc.run_plugin_chat_command("c", "", session_id)
    assert isinstance(req, sc.PluginFormRequest) and req.callback_id
    return req


async def test_pending_form_survives_slash_commands_reimport(monkeypatch) -> None:
    """A form opened before the module re-import is still submittable after it —
    with session scoping and single-use semantics intact across the boundary."""
    from graph import slash_commands as before

    req = await _open_form(monkeypatch, "sess1")

    # Simulate what a plugin reload does to the module: drop it from sys.modules
    # and import it fresh — a NEW module object whose top level re-executed.
    saved = sys.modules.pop("graph.slash_commands")
    try:
        after = importlib.import_module("graph.slash_commands")
        assert after is not before  # genuinely a fresh module, not the cached one

        # Session scoping still refuses a foreign submit WITHOUT consuming it…
        foreign = await after.submit_plugin_form(req.callback_id, {}, "intruder")
        assert foreign.startswith("⚠️") and "different session" in foreign

        # …and the legit owner's submit reaches the pre-reload on_submit closure.
        reply = await after.submit_plugin_form(req.callback_id, {"name": "Ada"}, "sess1")
        assert reply == "done:Ada"

        # Single-use spans both module generations: the OLD module sees it consumed too.
        again = await before.submit_plugin_form(req.callback_id, {}, "sess1")
        assert again.startswith("⚠️") and "expired" in again
    finally:
        # Restore the original module (and the package attribute the fresh import
        # rebound) so later tests keep their class identities.
        sys.modules["graph.slash_commands"] = saved
        import graph

        graph.slash_commands = saved


async def test_ttl_reaping_spans_the_reimport(monkeypatch) -> None:
    """The TTL reaper in a freshly imported module reaps entries registered by the
    old one — same dict, unchanged reaping logic."""
    from graph import _form_state
    from graph import slash_commands as sc

    req = await _open_form(monkeypatch, "s")
    store = _form_state.form_callbacks()
    store[req.callback_id].created -= sc._PLUGIN_FORM_TTL_S + 1  # age it past the TTL

    saved = sys.modules.pop("graph.slash_commands")
    try:
        after = importlib.import_module("graph.slash_commands")
        after._reap_stale_plugin_forms()
        assert req.callback_id not in store
        expired = await after.submit_plugin_form(req.callback_id, {}, "s")
        assert expired.startswith("⚠️") and "expired" in expired
    finally:
        sys.modules["graph.slash_commands"] = saved
        import graph

        graph.slash_commands = saved


async def test_form_state_store_survives_its_own_reimport() -> None:
    """The sys.modules anchor holds even if graph._form_state ITSELF is re-imported:
    form_callbacks() keeps returning the very same dict instance."""
    from graph import _form_state as before

    store = before.form_callbacks()
    store["sentinel"] = "still-here"
    saved = sys.modules.pop("graph._form_state")
    try:
        after = importlib.import_module("graph._form_state")
        assert after is not before
        assert after.form_callbacks() is store  # identity, not equality
        assert after.form_callbacks()["sentinel"] == "still-here"
    finally:
        store.pop("sentinel", None)
        sys.modules["graph._form_state"] = saved
        import graph

        graph._form_state = saved
