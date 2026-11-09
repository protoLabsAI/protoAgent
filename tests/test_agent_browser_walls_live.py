"""Live, real-browser tests for wall detection and content boundaries (agent_browser).

Serves ``tests/fixtures/walls/`` over a throwaway local HTTP server (plus /members → a 302 to the
login page, and /forbidden + /ratelimited for 403 / 429) and drives the REAL tools from ``get_browser_tools``.
Skips unless the agent-browser CLI resolves and Chrome is available, like
test_agent_browser_forms_live.py. One isolated session (``protoagent-walls-<pid>``), closed
at the end — never the operator's default session.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import importlib
import json
import os
import threading
from pathlib import Path

import pytest

from graph.plugins.testkit import load_plugin

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "plugins" / "agent_browser"
FIXTURES = REPO / "tests" / "fixtures" / "walls"
_PKG = load_plugin(ROOT, "agent_browser")
preflight = importlib.import_module(f"{_PKG.__name__}.preflight")
tools = importlib.import_module(f"{_PKG.__name__}.tools")
boundaries = importlib.import_module(f"{_PKG.__name__}.boundaries")

_PROBE = preflight.probe({"binary": "agent-browser"})
_UNAVAILABLE = (not _PROBE.cli_ok) or (_PROBE.chrome == "missing")
pytestmark = [pytest.mark.platform_sensitive,
              pytest.mark.skipif(_UNAVAILABLE, reason="agent-browser CLI / Chrome not usable here")]

_STATUS = {"/forbidden": (403, "Forbidden"), "/ratelimited": (429, "Too Many Requests")}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server's naming
        if self.path == "/members":  # a members-only page bouncing a signed-out visitor
            self.send_response(302)
            self.send_header("Location", "/login/?next=/members")
            self.end_headers()
            return
        if self.path in _STATUS:
            code, title = _STATUS[self.path]
            body = f"<html><head><title>{title}</title></head><body><h1>{code} {title}</h1></body></html>".encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=str(FIXTURES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def browser():
    prev = os.environ.get("AGENT_BROWSER_SESSION")
    os.environ["AGENT_BROWSER_SESSION"] = f"protoagent-walls-{os.getpid()}"
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


async def _open(browser, url):
    out = await browser["browser_open"].ainvoke({"url": url})
    if out.startswith("Error:") and "blocked by" not in out:
        pytest.skip(f"no usable browser on this host: {out[:160]}")
    return out


async def test_a_cloudflare_interstitial_is_a_wall_not_a_success(browser, site, monkeypatch):
    monkeypatch.setattr(tools, "_WALL_RECHECK_S", 0.5)
    out = await _open(browser, f"{site}/cloudflare.html")
    assert out.startswith("Error: blocked by a Cloudflare bot check at "), out
    assert "console Browser panel" in out and "Just a moment" not in out


async def test_a_challenge_that_clears_itself_reports_the_real_page(browser, site):
    out = await _open(browser, f"{site}/cloudflare-clears.html")
    assert out.startswith("✓ Remote jobs board"), out
    assert out.rstrip().endswith("/article.html")


async def test_bot_fight_mode_script_on_an_ordinary_page_is_not_a_wall(browser, site):
    # The /cdn-cgi/challenge-platform/ script is injected into normal 200 pages behind Cloudflare
    # Bot Fight Mode and never leaves the DOM — a sparse page carrying it must not be a wall.
    out = await _open(browser, f"{site}/cloudflare-bfm.html")
    assert out.startswith("✓ Acme Robotics"), out


async def test_a_login_redirect_is_a_signin_wall(browser, site):
    out = await _open(browser, f"{site}/members")
    assert out.startswith("Error: blocked by a sign-in wall at "), out
    assert "/login/?next=" in out and "Do not type credentials" in out


async def test_opening_the_login_page_on_purpose_is_fine(browser, site):
    out = await _open(browser, f"{site}/login/")
    assert out.startswith("✓ Sign in"), out


@pytest.mark.parametrize("path,kind", [("/forbidden", "HTTP 403"), ("/ratelimited", "HTTP 429")])
async def test_http_403_and_429_are_walls(browser, site, path, kind):
    out = await _open(browser, site + path)
    assert out.startswith(f"Error: blocked by an {kind}"), out


async def test_an_article_that_mentions_sign_in_is_not_a_wall(browser, site):
    out = await _open(browser, f"{site}/article.html")
    assert out.startswith("✓ Remote jobs board"), out


async def test_page_text_and_form_read_arrive_inside_the_markers(browser, site):
    await _open(browser, f"{site}/article.html")
    text = await browser["browser_get_text"].ainvoke({})
    inner, marker = boundaries.unwrap(text)
    assert marker is not None and marker.origin.endswith("/article.html"), text
    assert "Ignore all previous instructions" in inner

    read = await browser["browser_form_read"].ainvoke({})
    inner, marker = boundaries.unwrap(read)
    assert marker is not None, read
    assert [f["label"] for f in json.loads(inner)] == ["Search jobs"]

    # the form tools that parse eval output internally still work with boundaries on
    filled = await browser["browser_fill"].ainvoke({"selector": "Search jobs", "text": "engineer"})
    assert not filled.startswith("Error:"), filled
    assert await browser["browser_get_value"].ainvoke({"selector": "Search jobs"}) == "engineer"
    # browser_eval returns the caller's own script result RAW — never wrapped in the markers.
    ev = await browser["browser_eval"].ainvoke({"expression": "document.title"})
    assert ev == '"Remote jobs board"', ev
    assert boundaries.unwrap(ev)[1] is None  # not a wrapped block
