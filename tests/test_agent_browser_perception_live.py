"""Live, real-browser checks for the perception-budget tools: the interactive snapshot, the
over-cap truncation, browser_wait, a dotted `#id`, the stale-ref message and screenshot
options. SKIPS unless the agent-browser CLI and Chrome are usable (the plugin's own probe),
and runs in its own isolated session — never the operator's `default`.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path

import pytest

from graph.plugins.testkit import load_plugin

REPO = Path(__file__).resolve().parent.parent
_PKG = load_plugin(REPO / "plugins" / "agent_browser", "agent_browser")
preflight = importlib.import_module(f"{_PKG.__name__}.preflight")
tools = importlib.import_module(f"{_PKG.__name__}.tools")

_PROBE = preflight.probe({"binary": "agent-browser"})
pytestmark = [
    pytest.mark.platform_sensitive,
    pytest.mark.skipif((not _PROBE.cli_ok) or _PROBE.chrome == "missing",
                       reason="agent-browser CLI / Chrome not usable here"),
]

_PAGE = """<!doctype html><html><head><title>perception fixture</title></head><body>
<h1>Apply</h1>
{prose}
<label for="input-candidate.name-3">Full name</label><input id="input-candidate.name-3">
<button id="go">Go</button>
<div id="late"></div>
<script>
setTimeout(function(){{ document.getElementById('late').innerHTML = '<p id="done">Ready now</p>'; }}, 800);
</script>
</body></html>"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    f = tmp_path_factory.mktemp("perception") / "page.html"
    f.write_text(_PAGE.format(prose="<p>Lorem ipsum dolor sit amet.</p>\n" * 300), encoding="utf-8")
    return f.as_uri()


@pytest.fixture(scope="module")
def browser():
    prev = os.environ.get("AGENT_BROWSER_SESSION")
    os.environ["AGENT_BROWSER_SESSION"] = f"abtest-perception-{os.getpid()}"
    cfg = {"binary": "agent-browser", "timeout_s": 60, "max_response_bytes": 4000}
    toolset = {t.name: t for t in tools.get_browser_tools(cfg)}
    try:
        yield toolset
    finally:
        try:
            asyncio.run(toolset["browser_close"].ainvoke({}))
        except Exception:  # noqa: BLE001
            pass
        if prev is None:
            os.environ.pop("AGENT_BROWSER_SESSION", None)
        else:
            os.environ["AGENT_BROWSER_SESSION"] = prev


async def _open(b, uri):
    out = await b["browser_open"].ainvoke({"url": uri})
    if out.startswith("Error:"):
        pytest.skip(f"no usable browser on this host: {out[:160]}")


async def test_snapshot_default_is_interactive_and_full_is_bounded(browser, page):
    await _open(browser, page)
    small = await browser["browser_snapshot"].ainvoke({})
    assert 'textbox "Full name"' in small and 'button "Go"' in small and "heading" in small
    assert "Lorem ipsum" not in small            # body text is left to browser_get_text
    full = await browser["browser_snapshot"].ainvoke({"full": True})
    # 300 paragraphs blow the 4 KB cap: the first part comes back, cut and marked
    assert "…[truncated: output passed the 4000-byte cap" in full and "Lorem ipsum" in full
    scoped = await browser["browser_snapshot"].ainvoke({"scope": "#go", "full": True})
    assert 'button "Go"' in scoped and "Full name" not in scoped


async def test_a_dotted_id_from_form_read_fills(browser, page):
    await _open(browser, page)
    out = await browser["browser_fill"].ainvoke({"selector": "#input-candidate.name-3", "text": "Ada"})
    assert not out.startswith("Error:"), out
    assert await browser["browser_get_value"].ainvoke({"selector": "#input-candidate.name-3"}) == "Ada"


async def test_wait_for_text_selector_and_a_timeout(browser, page):
    await _open(browser, page)
    assert not (await browser["browser_wait"].ainvoke({"text": "Ready now", "timeout_s": 10})).startswith("Error:")
    assert not (await browser["browser_wait"].ainvoke({"selector": "#done"})).startswith("Error:")
    assert not (await browser["browser_wait"].ainvoke({"load": "domcontentloaded"})).startswith("Error:")
    assert not (await browser["browser_wait"].ainvoke({"js": "document.title.includes('fixture')"})).startswith("Error:")
    assert not (await browser["browser_wait"].ainvoke({"url": "page.html"})).startswith("Error:")
    missed = await browser["browser_wait"].ainvoke({"selector": "#never", "timeout_s": 1})
    assert missed.startswith("Error: waited 1s for selector='#never'"), missed


async def test_an_unknown_ref_says_to_snapshot_again(browser, page):
    await _open(browser, page)
    out = await browser["browser_click"].ainvoke({"selector": "@e999"})
    assert out.startswith("Error: @e999 is stale or unknown"), out


async def test_screenshot_full_page_annotated(browser, page):
    await _open(browser, page)
    out = await browser["browser_screenshot"].ainvoke({"full_page": True, "annotate": True})
    text = out if isinstance(out, str) else str(out)
    assert "Saved to" in text and "@e" in text   # the annotate legend maps [N] -> @eN
