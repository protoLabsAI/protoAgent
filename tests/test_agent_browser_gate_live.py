"""Live, real-browser tests for the browser action gate (plugins/agent_browser/gate.py).

The unit tests (tests/test_agent_browser_gate.py) script the CLI, so the in-page probe never
runs there. Here it does: the REAL tools, the REAL agent-browser CLI and Chrome, a local form
fixture over ``file://``. A ref-addressed click on the real Submit button is intercepted (and
the form is NOT submitted), a non-submit click goes through, and an approved submit runs.

Skips unless the CLI resolves and Chrome is present (the plugin's own preflight decides), like
tests/test_agent_browser_forms_live.py. Runs in its OWN agent-browser session, never the
operator's: ``protoagent-gate-<pid>``, or ``PROTOAGENT_LIVE_BROWSER_SESSION`` when set.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
from pathlib import Path

import pytest

from graph.plugins.testkit import load_plugin

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "plugins" / "agent_browser"
APPLY = (REPO / "tests" / "fixtures" / "gate" / "apply.html").as_uri()

_PKG = load_plugin(ROOT, "agent_browser")
preflight = importlib.import_module(f"{_PKG.__name__}.preflight")
tools = importlib.import_module(f"{_PKG.__name__}.tools")
gate = importlib.import_module(f"{_PKG.__name__}.gate")

_PROBE = preflight.probe({"binary": "agent-browser"})
_UNAVAILABLE = (not _PROBE.cli_ok) or (_PROBE.chrome == "missing")
_REASON = "agent-browser CLI is not usable here" if not _PROBE.cli_ok else "no Chrome for agent-browser to drive"
pytestmark = [pytest.mark.platform_sensitive, pytest.mark.skipif(_UNAVAILABLE, reason=_REASON)]


@pytest.fixture(scope="module")
def browser():
    prev = os.environ.get("AGENT_BROWSER_SESSION")
    os.environ["AGENT_BROWSER_SESSION"] = os.environ.get("PROTOAGENT_LIVE_BROWSER_SESSION") or f"protoagent-gate-{os.getpid()}"
    # The default config: the gate is on for `submit`.
    toolset = {t.name: t for t in tools.get_browser_tools({"binary": "agent-browser", "timeout_s": 120})}
    try:
        yield toolset
    finally:
        try:
            asyncio.run(toolset["browser_close"].ainvoke({}))
        except Exception:  # noqa: BLE001 — a failed close must not fail the suite
            pass
        if prev is None:
            os.environ.pop("AGENT_BROWSER_SESSION", None)
        else:
            os.environ["AGENT_BROWSER_SESSION"] = prev


async def _open(toolset):
    out = await toolset["browser_open"].ainvoke({"url": APPLY})
    if out.startswith("Error:"):
        pytest.skip(f"no usable browser on this host: {out[:160]}")


async def _ref(toolset, pattern: str) -> str:
    snap = await toolset["browser_snapshot"].ainvoke({})
    m = re.search(pattern + r'.*?\[ref=(e\d+)\]', snap)
    assert m, snap
    return "@" + m.group(1)


async def _count(toolset, var: str) -> str:
    return (await toolset["browser_eval"].ainvoke({"expression": f"String(window.{var} || 0)"})).strip().strip('"')


async def test_a_ref_click_on_submit_is_intercepted_and_a_non_submit_click_is_not(browser):
    await _open(browser)
    submit = await _ref(browser, r'button "Submit application"')
    out = await browser["browser_click"].ainvoke({"selector": submit})
    # No operator chat turn here (no graph), so it's refused rather than parked.
    assert out.startswith("Error: blocked by the browser action gate (submit)"), out
    assert "button “Submit application”" in out
    assert await _count(browser, "__submits") == "0"  # the form was NOT submitted

    other = await _ref(browser, r'button "Add another"')
    assert not (await browser["browser_click"].ainvoke({"selector": other})).startswith("Error:")
    assert await _count(browser, "__added") == "1"

    # "Apply now" opens a form; it's never gated.
    apply_now = await _ref(browser, r'link "Apply now"')
    assert not (await browser["browser_click"].ainvoke({"selector": apply_now})).startswith("Error:")


async def test_css_click_and_enter_in_a_field_are_gated_too(browser):
    await _open(browser)
    out = await browser["browser_click"].ainvoke({"selector": "#sub"})
    assert out.startswith("Error: blocked by the browser action gate (submit)"), out
    await browser["browser_click"].ainvoke({"selector": "#email"})
    out = await browser["browser_press"].ainvoke({"key": "Enter"})
    assert out.startswith("Error: blocked by the browser action gate (submit)") and "Email" in out, out
    # Enter in the search box is a search, not a submission
    await browser["browser_click"].ainvoke({"selector": "#search input"})
    assert not (await browser["browser_press"].ainvoke({"key": "Enter"})).startswith("Error:")
    await _open(browser)
    assert await _count(browser, "__submits") == "0"


async def test_an_approved_submit_runs(browser, monkeypatch):
    import langgraph.types as lgt
    await _open(browser)
    submit = await _ref(browser, r'button "Submit application"')
    cards = []
    monkeypatch.setattr(gate, "interrupt_context", lambda state: (True, "", "live|tools:1"))
    monkeypatch.setattr(lgt, "interrupt", lambda card: cards.append(card) or "approve")
    out = await browser["browser_click"].ainvoke({"selector": submit})
    assert not out.startswith("Error:"), out
    assert cards and cards[0]["description"].startswith("Submit the form") and "Submit application" in cards[0]["description"]
    assert await _count(browser, "__submits") == "1"
