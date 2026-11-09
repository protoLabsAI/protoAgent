"""Live, real-browser tests for the plugin browser client (plugins/agent_browser/client.py).

Serves ``tests/fixtures/walls/`` over a throwaway local HTTP server and drives a REAL
``BrowserClient`` (built through the registered ``agent_browser.client`` service) on its own
isolated session (``abtest-client-<pid>``), closed at the end — never an operator's session.
Skips unless the agent-browser CLI resolves and Chrome is available, like the other *_live tests.
"""

from __future__ import annotations

import functools
import http.server
import importlib
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from graph.plugins.testkit import FakeRegistry, load_plugin

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "walls"
_PKG = load_plugin(REPO / "plugins" / "agent_browser", "agent_browser")
preflight = importlib.import_module(f"{_PKG.__name__}.preflight")
client_mod = importlib.import_module(f"{_PKG.__name__}.client")

_PROBE = preflight.probe({"binary": "agent-browser"})
_UNAVAILABLE = (not _PROBE.cli_ok) or (_PROBE.chrome == "missing")
pytestmark = [pytest.mark.platform_sensitive,
              pytest.mark.skipif(_UNAVAILABLE, reason="agent-browser CLI / Chrome not usable here")]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(FIXTURES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def browser():
    reg = FakeRegistry(config={"binary": "agent-browser"}, plugin_id="agent_browser")
    _PKG.register(reg)
    make = reg.services["agent_browser.client"]
    b = make(session=f"abtest-client-{os.getpid()}", instance_scoped=False, own_tab="cltab",
             timeout_s=30, nav_timeout_s=40)
    try:
        yield b
    finally:
        b.close()


def _data(html: str) -> str:
    return "data:text/html," + quote(html)


def test_open_lands_in_its_own_tab_and_leaves_other_tabs_alone(browser, site):
    # Another tab (standing in for the operator's) is active first …
    other = client_mod.BrowserClient(session=browser.session, instance_scoped=False, host=browser._host)
    first = other.open(f"{site}/article.html")
    if first.kind == "unavailable" or (not first.ok and "Chrome" in first.error):
        pytest.skip(f"no usable browser on this host: {first.error[:160]}")
    assert first.ok, first
    # … then the client navigates only its labelled tab.
    r = browser.open(f"{site}/login/")
    assert r.ok and r.value["title"].startswith("Sign in"), r
    tabs = browser.tabs().value
    mine = [t for t in tabs if t.get("label") == "cltab"]
    assert len(mine) == 1 and mine[0]["active"] and mine[0]["url"].endswith("/login/")
    assert any(t["url"].endswith("/article.html") and t.get("label") != "cltab" for t in tabs)
    assert browser.focus_tab().value == "switched"  # reused, not re-created


def test_a_cloudflare_interstitial_is_a_typed_wall(browser, site, monkeypatch):
    monkeypatch.setattr(client_mod, "_WALL_RECHECK_S", 0.5)
    r = browser.open(f"{site}/cloudflare.html")
    assert r.kind == "wall" and r.wall.kind == "cloudflare", r


def test_a_challenge_that_clears_itself_reports_the_real_page(browser, site):
    r = browser.open(f"{site}/cloudflare-clears.html")
    assert r.ok and r.value["url"].endswith("/article.html"), r


def test_wait_for_late_content_and_a_typed_timeout_for_absent_content(browser):
    late = "<title>Late</title><body><script>setTimeout(()=>{document.body.innerHTML='<li class=card>x</li>'},800)</script></body>"
    assert browser.open(_data(late), check_walls=False).ok
    assert browser.wait(selector="li.card", timeout_s=8).ok
    started = time.monotonic()
    r = browser.wait(selector="#never", timeout_s=1.5)
    assert r.kind == "timeout" and "#never" in r.error, r
    # One daemon-side timeout, not the CLI's five 30 s re-sends.
    assert time.monotonic() - started < 10


def test_eval_json_and_get_text(browser):
    assert browser.open(_data("<title>T</title><main><p>Hello there</p></main>"), check_walls=False).ok
    r = browser.eval_json("JSON.stringify({title: document.title, n: 2})")
    assert r.ok and r.value == {"title": "T", "n": 2}, r
    assert browser.eval_json("throw new Error('nope')").kind == "script_error"
    assert "Hello there" in browser.get_text("main").text
    assert browser.snapshot().ok


def test_a_url_less_open_is_refused(browser):
    assert browser.open("").kind == "bad_argument"
