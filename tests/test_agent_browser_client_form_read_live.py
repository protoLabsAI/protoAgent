"""Live: the plugin client's ``form_read`` on the agent's own session reads what the tool reads.

The agent's ``browser_*`` tools open and fill the saved Greenhouse fixture
(``tests/fixtures/ats/greenhouse.html``, ``file://``, no network) on an ISOLATED session
(``abtest-formread-<pid>`` — never an operator's), then a client built with
``make.for_agent_session(state)`` reads that same page back. Its ``value`` must equal the field
list ``browser_form_read`` renders, minus the content-boundary markers — the read careercoach's
verify step relies on instead of a read-back the model passes in. Skips without a usable CLI/Chrome.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path

import pytest

from graph.plugins.testkit import FakeRegistry, load_plugin

REPO = Path(__file__).resolve().parent.parent
GREENHOUSE = (REPO / "tests" / "fixtures" / "ats" / "greenhouse.html").as_uri()
_PKG = load_plugin(REPO / "plugins" / "agent_browser", "agent_browser")
_mod = importlib.import_module
preflight = _mod(f"{_PKG.__name__}.preflight")
tools = _mod(f"{_PKG.__name__}.tools")
boundaries = _mod(f"{_PKG.__name__}.boundaries")

_PROBE = preflight.probe({"binary": "agent-browser"})
_UNAVAILABLE = (not _PROBE.cli_ok) or (_PROBE.chrome == "missing")
pytestmark = [pytest.mark.platform_sensitive,
              pytest.mark.skipif(_UNAVAILABLE, reason="agent-browser CLI / Chrome not usable here")]

# The idle reaper is a backstop for a KILLED run, which never reaches the close below.
_CFG = {"binary": "agent-browser", "timeout_s": 120, "main_idle_timeout_min": 3}


@pytest.fixture(scope="module")
def session():
    name = f"abtest-formread-{os.getpid()}"
    prev = os.environ.get("AGENT_BROWSER_SESSION")
    os.environ["AGENT_BROWSER_SESSION"] = name  # session_scope: shared → the tools use this name
    toolset = {t.name: t for t in tools.get_browser_tools(dict(_CFG))}
    reg = FakeRegistry(config=dict(_CFG), plugin_id="agent_browser")
    _PKG.register(reg)
    try:
        yield name, toolset, reg.services["agent_browser.client"]
    finally:
        try:
            asyncio.run(toolset["browser_close"].ainvoke({}))
        except Exception:  # noqa: BLE001 — a failed close must not fail the suite
            pass
        if prev is None:
            os.environ.pop("AGENT_BROWSER_SESSION", None)
        else:
            os.environ["AGENT_BROWSER_SESSION"] = prev


async def test_form_read_on_the_agent_session_matches_browser_form_read(session):
    name, toolset, make = session
    out = await toolset["browser_open"].ainvoke({"url": GREENHOUSE})
    if out.startswith("Error:"):
        pytest.skip(f"no usable browser on this host: {out[:160]}")
    filled = await toolset["browser_fill"].ainvoke({"selector": "First Name", "text": "Ada"})
    assert not filled.startswith("Error:"), filled

    browser = make.for_agent_session({"session_id": "chat-under-test"})
    assert browser.session == name and browser.agent_session

    r = await asyncio.to_thread(browser.form_read)
    assert r.ok, r
    tool_raw = await toolset["browser_form_read"].ainvoke({"scope": ""})
    assert not tool_raw.startswith("Error:"), tool_raw
    assert r.value == json.loads(boundaries.strip(tool_raw))  # same reader, same shape, no markers

    by_label = {f["label"]: f for f in r.value}
    assert by_label["First Name"]["value"] == "Ada" and by_label["First Name"]["required"]
    visa = by_label["Are you authorized to work?"]
    assert visa["kind"] == "combobox" and "Yes" in visa["options"]  # the react-select option probe
    assert set(r.value[0]) >= {"label", "kind", "name", "id", "required", "value"}

    scoped = await asyncio.to_thread(browser.form_read, "#application")
    assert scoped.ok and [f["label"] for f in scoped.value] == [f["label"] for f in r.value]
    missing = await asyncio.to_thread(browser.form_read, "#no-such-form")
    assert missing.kind == "cli_error" and "#no-such-form" in missing.error
    assert browser.close().kind == "bad_argument"  # the agent's browser isn't the plugin's to close
