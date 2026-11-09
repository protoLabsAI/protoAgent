"""The standalone page for a RENDERED artifact: open_artifact's browser link for html/react/
markdown/mermaid/svg/vega-lite (the design-mockups workflow hands it to the operator).

The page is the shell itself in a `standalone` mode, served on the PUBLIC prefix, so it must gate
itself: an expiring core media signature bound to the artifact id, version, kind and source. A
bad, missing, expired or other-version ticket is the same bare 404, and the generated code only
ever runs in the shell's nested sandbox.
"""

import json
import re
import time
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from tests.test_artifact_media import _client, _save
from tests.test_artifact_open import _url, artifact  # noqa: F401 — the fixture

pytestmark = pytest.mark.platform_sensitive


def _show(art, kind="html", code="<p>hello</p>", title="Mockup"):
    art.show_artifact.invoke({"kind": kind, "code": code, "title": title})
    return art._read_store()["artifacts"][0]["id"]


def _page_payload(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="__artpage">(.*?)</script>', html, re.S)
    assert m, "the page embeds its version as inert JSON"
    return json.loads(m[1])


# ── the link and the page ─────────────────────────────────────────────────────


def test_open_artifact_on_html_gives_a_page_link(artifact, monkeypatch):  # noqa: F811
    aid = _show(artifact, code="<main class='pl-card'>Directive ledger</main>")
    events = []
    monkeypatch.setattr(artifact._store, "_emit", lambda topic, data: events.append((topic, data)))
    reply = artifact.open_artifact.invoke({"artifact_id": aid, "state": {"session_id": "chat"}})
    url = _url(reply)
    parts = urlsplit(url)
    assert parts.path == f"/plugins/artifact/media/artifact/{aid}/page"
    # Same event the file link emits, so the console opens the tab exactly as today.
    assert events == [("open_url", {"session_id": "chat", "id": aid, "path": url.removeprefix("http://testserver")})]
    assert "token=" not in url and "bearer" not in url.lower()
    q = parse_qs(parts.query)
    assert q["version"] == ["1"] and len(q["t"][0]) == 64
    # The same lifetime as the file open link (and the text the tool prints says so).
    assert abs(int(q["exp"][0]) - (time.time() + artifact._media.TTL_S)) < 30
    assert f"expires in {artifact._media.TTL_S // 60} minutes" in reply

    r = _client(artifact).get(url)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert '<html class="standalone">' in page  # no ?theme → no data-theme → prefers-color-scheme
    assert "<title>Mockup</title>" in page
    assert _page_payload(page) == {
        "id": aid, "kind": "html", "title": "Mockup", "code": "<main class='pl-card'>Directive ledger</main>",
        "version": 1, "n": 1, "ts": artifact._read_store()["artifacts"][0]["versions"][0]["ts"],
    }
    assert '<script type="module" src="../../../shell.js">' in page  # resolves to /plugins/artifact/shell.js
    for k, v in artifact._routes.PAGE_HEADERS.items():
        assert r.headers[k] == v


def test_no_console_still_returns_the_link(artifact):  # noqa: F811
    aid = _show(artifact)
    reply = artifact.open_artifact.invoke({"artifact_id": aid, "state": {"renders_ui": False}})
    assert "No console attached" in reply and "/page?" in _url(reply)


@pytest.mark.parametrize(
    "kind,code",
    [
        ("markdown", "# Knowledge collections\n\nThree collections."),
        ("mermaid", "graph TD; A-->B"),
        ("svg", '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'),
        ("react", "export default function App(){ return <p>hi</p>; }"),
        ("vega-lite", json.dumps({"mark": "bar", "data": {"values": [{"a": 1}]}, "encoding": {}})),
    ],
)
def test_every_rendered_kind_opens_as_a_page(artifact, kind, code):  # noqa: F811
    aid = _show(artifact, kind=kind, code=code)
    r = _client(artifact).get(_url(artifact.open_artifact.invoke({"artifact_id": aid})))
    assert r.status_code == 200 and _page_payload(r.text)["kind"] == kind


@pytest.mark.parametrize("theme,attr", [("light", ' data-theme="light"'), ("dark", ' data-theme="dark"'),
                                         ("system", ""), ('x"><script>', "")])
def test_theme_query_forces_only_known_palettes(artifact, theme, attr):  # noqa: F811
    aid = _show(artifact)
    url = _url(artifact.open_artifact.invoke({"artifact_id": aid}))
    page = _client(artifact).get(url + "&theme=" + quote(theme)).text
    assert f'<html class="standalone"{attr}>' in page
    assert "x\"><script>" not in page


# ── the generated code stays in the sandbox ───────────────────────────────────


def test_artifact_source_cannot_break_out_into_the_shell_page(artifact):  # noqa: F811
    hostile = '</script><script>alert(document.cookie)</script><!-- &  '
    aid = _show(artifact, code=hostile, title="</title><script>alert(1)</script>")
    page = _client(artifact).get(_url(artifact.open_artifact.invoke({"artifact_id": aid}))).text
    shell = artifact._shell._SHELL_HTML
    # Not one extra element boundary: the source and the title are data, never markup.
    assert page.count("</script>") == shell.count("</script>") + 1  # + the JSON element's own closer
    assert page.count("<script") == shell.count("<script") + 1
    assert "<script>alert" not in page and "alert(document.cookie)" not in page.split('id="__artpage">')[0]
    assert "<title>&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;</title>" in page
    assert _page_payload(page)["code"] == hostile  # JSON.parse gives the exact source back


def test_page_keeps_the_nested_sandbox(artifact):  # noqa: F811
    # The artifact still renders in the shell's nested frame: script-only sandbox, opaque origin
    # (no allow-same-origin), no top navigation, no popups — the same frame as the panel.
    aid = _show(artifact)
    page = _client(artifact).get(_url(artifact.open_artifact.invoke({"artifact_id": aid}))).text
    frames = re.findall(r"<iframe[^>]*>", page)
    assert frames == ['<iframe id="frame" sandbox="allow-scripts allow-pointer-lock" referrerpolicy="no-referrer">']
    assert "allow-same-origin" not in page and "allow-top-navigation" not in page and "allow-popups" not in page


def test_shell_standalone_mode_reaches_no_gated_route():
    # Source guards on shell.js: on a standalone page the bridge (ask/send/openLink) is refused
    # locally, render verdicts aren't POSTed, and boot renders the embedded version without the
    # store poll. (The page has no bearer, so these would only 401 — but they must not try.)
    from tests.test_artifact_plugin import ROOT

    js = (ROOT / "shell.js").read_text()
    assert 'if(PAGE) return;  // standalone page: no bearer' in js
    assert 'is unavailable on a standalone page' in js
    assert "if (PAGE) { renderPage(); return; }" in js
    render_page = js[js.index("function renderPage(){"):js.index("// Boot ONCE")]
    assert "apiFetch" not in render_page and "frameNav.set(srcdoc(PAGE.kind, PAGE.code, null)" in render_page


def test_shell_html_anchors_the_page_route_rewrites():
    # _page_html rewrites three exact anchors in shell.html; if one moves, the page would serve
    # the panel (or 404 its module) instead of failing loudly.
    from tests.test_artifact_plugin import ROOT

    html = (ROOT / "shell.html").read_text()
    # The FIRST "<html>" (the one replaced) is the root element; later ones are in comments.
    assert html.startswith("<!doctype html><html><head><meta charset=\"utf-8\">")
    assert html.count('<meta charset="utf-8">') == 1
    assert html.count('<script type="module" src="shell.js">') == 1


# ── the ticket ────────────────────────────────────────────────────────────────


def test_never_serves_without_a_valid_ticket(artifact):  # noqa: F811
    aid = _show(artifact)
    url = _url(artifact.open_artifact.invoke({"artifact_id": aid}))
    path, q = urlsplit(url).path, parse_qs(urlsplit(url).query)
    c = _client(artifact)
    exp, t = q["exp"][0], q["t"][0]
    flipped = t[:-1] + ("0" if t[-1] != "0" else "1")
    bad = [
        {},  # no ticket at all
        {"version": 1},
        {"version": 1, "exp": exp},  # no signature
        {"version": 1, "t": t},  # no expiry
        {"version": 1, "exp": exp, "t": flipped},  # forged signature
        {"version": 1, "exp": exp, "t": "é" * 64},
        {"version": 1, "exp": exp, "t": t.upper()},
        {"version": 1, "exp": str(int(exp) + 60), "t": t},  # stretched expiry (it's signed)
        {"version": 1, "exp": "1", "t": t},  # expired
        {"version": 0, "exp": exp, "t": t},  # "latest" is never a grant
        {"version": 2, "exp": exp, "t": t},  # a version that doesn't exist
    ]
    for params in bad:
        r = c.get(path, params=params)
        assert r.status_code == 404 and r.content == b"", params
    assert c.get("/plugins/artifact/media/artifact/other/page", params={"version": 1, "exp": exp, "t": t}).status_code == 404
    assert c.get(url).status_code == 200  # …and the real one still works


def test_expired_ticket_is_404(artifact, monkeypatch):  # noqa: F811
    aid = _show(artifact)
    url = _url(artifact.open_artifact.invoke({"artifact_id": aid}))
    c = _client(artifact)
    assert c.get(url).status_code == 200
    real = time.time
    monkeypatch.setattr(artifact._routes.time, "time", lambda: real() + artifact._media.TTL_S + 5)
    assert c.get(url).status_code == 404


def test_ticket_is_bound_to_its_version(artifact):  # noqa: F811
    aid = _show(artifact, code="<p>v1</p>")
    artifact.update_artifact.invoke({"artifact_id": aid, "old_string": "v1", "new_string": "v2"})
    c = _client(artifact)
    v1 = _url(artifact.open_artifact.invoke({"artifact_id": aid, "version": 1}))
    latest = _url(artifact.open_artifact.invoke({"artifact_id": aid}))
    assert _page_payload(c.get(v1).text)["code"] == "<p>v1</p>"
    assert _page_payload(c.get(latest).text)["code"] == "<p>v2</p>"
    assert "version=2" in latest
    # A v1 grant never opens v2 (or the reverse).
    assert c.get(v1.replace("version=1", "version=2")).status_code == 404
    assert c.get(latest.replace("version=2", "version=1")).status_code == 404
    # A version-cap trim shifts v2 onto position 1: the old position-1 grant must not show it.
    store = artifact._read_store()
    store["artifacts"][0]["versions"].pop(0)
    artifact._write_store(store)
    assert c.get(v1).status_code == 404


def test_render_stamp_keeps_the_link_valid(artifact):  # noqa: F811
    aid = _show(artifact)
    url = _url(artifact.open_artifact.invoke({"artifact_id": aid}))
    c = _client(artifact)
    stamp = c.post("/api/plugins/artifact/render-status", json={"id": aid, "version": 1, "ok": True})
    assert stamp.json()["recorded"] is True
    assert c.get(url).status_code == 200


def test_page_and_blob_grants_never_cross(artifact, tmp_path):  # noqa: F811
    from infra.media import sign_name

    fid = _save(artifact, tmp_path, "report.txt", b"file bytes")
    hid = _show(artifact)
    c = _client(artifact)
    blob = urlsplit(_url(artifact.open_artifact.invoke({"artifact_id": fid})))
    page = urlsplit(_url(artifact.open_artifact.invoke({"artifact_id": hid})))
    # A file artifact has no page, even with a blob grant for it…
    assert c.get(f"/plugins/artifact/media/artifact/{fid}/page?{blob.query}").status_code == 404
    # …or a page-shaped signature over it (the route refuses file artifacts outright).
    art = artifact._read_store()
    farts = next(a for a in art["artifacts"] if a["id"] == fid)
    exp = int(time.time()) + 600
    forged = sign_name(artifact._routes._page_ticket_subject(fid, 1, "file", farts["versions"][0]), exp)
    assert c.get(f"/plugins/artifact/media/artifact/{fid}/page", params={"version": 1, "exp": exp, "t": forged}).status_code == 404
    # A page grant never downloads through the blob route.
    assert c.get(f"/plugins/artifact/media/artifact/{hid}/blob?{page.query}").status_code == 404
