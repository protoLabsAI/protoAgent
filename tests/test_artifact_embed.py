"""Chrome-less embed placement of the artifact shell (ADR 0118 D2).

``/plugins/artifact/view?embed=<id>&v=<version>`` renders exactly ONE artifact version with no
panel chrome, through the SAME ``srcdoc()`` frame builder the panel uses — so its nonce CSP,
vendored SRI LIB map, theme tokens, loader lockdown and render-verdict reporting are all
inherited, and there is no second frame builder. The embed frame reports its content height — a
reporter appended to EVERY embed frame by ``embedSuffix`` (NOT by ``base()``, which only the
scripted kinds reach), measuring the content BOX so the height can shrink as well as grow — and
the shell relays it to the console host. An unknown id/version is an inert "unavailable" state,
never an error page.

The in-frame height reporter and the version-resolution helper are run under ``node`` where it's
on PATH, so they're checked as BEHAVIOUR, not just as strings; those cases skip cleanly without
node. A real browser render is the live check in the PR."""

from __future__ import annotations

import pytest

from tests.test_artifact_plugin import _app, _load
from tests.test_artifact_slides import NODE, _js_function, _node

# node subprocesses + the shell's platform-sensitive deps → platform-sensitive.
pytestmark = pytest.mark.platform_sensitive

INLINE_KINDS = ["html", "svg", "mermaid", "react", "vega-lite"]


def _js(art) -> str:
    return art._SHELL_JS


def _assignment(js: str, name: str) -> str:
    """The ``var <name> = '…';`` statement text for a string-concatenation constant. The value
    ends with a ``<\\/script>`` escape, so the only ``'`` immediately followed by ``;`` is the
    statement terminator (inner lines end ``;'``); slice to there."""
    start = js.index(f"var {name} =")
    end = js.index("';\n", start) + len("';")
    return js[start:end]


# ── r1: one frame builder, shared verbatim by the panel and the embed ────────────


def test_there_is_exactly_one_frame_builder(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    assert js.count("function srcdoc(") == 1, "embed must reuse srcdoc(), never fork a second builder"
    # srcdoc takes no placement argument: the exact same call produces the exact same frame
    # (hence CSP) for the panel and the embed. Placement can't reach inside it.
    assert "function srcdoc(kind, code, links)" in js
    src = _js_function(js, "srcdoc")
    assert "EMBED" not in src, "srcdoc() must not branch on placement — its CSP is placement-independent"


def test_embed_renders_through_the_same_builder_as_the_panel(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    panel = _js_function(js, "render")
    embed = _js_function(js, "renderEmbed")
    # Both placements build the frame with the IDENTICAL expression — so the embed frame's CSP,
    # vendored libs and SRI are byte-identical to the panel's for every kind.
    builder = 'a.kind==="file" ? fileCard(v) : srcdoc(a.kind, v.code, renderingLinks)'
    assert builder in panel, "the panel builds the frame via srcdoc()/fileCard()"
    assert builder in embed, "the embed must build the frame via the SAME srcdoc()/fileCard() call"
    # The embed path invents no CSP/sandbox of its own.
    assert "Content-Security-Policy" not in embed and "sandbox" not in embed


def test_each_inline_kind_dispatches_without_a_placement_branch(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    src = _js_function(_js(art), "srcdoc")
    # Each of the five inline kinds has a single placement-independent branch in srcdoc().
    for kind in INLINE_KINDS:
        assert f'kind === "{kind}"' in src, kind
    # html routes through the prologue-aware htmlDoc() builder and carries the curated ESM import
    # map (ADR 0118 D6), merged with any map the author ships (htmlImportMap) — still one
    # placement-independent branch, identical for the panel and the embed.
    assert "var im = htmlImportMap(code);" in src
    assert "return htmlDoc(im.code, dsLink() + base(kind) + (im.map ? " in src
    assert 'return vegaDoc(code)' in src


def test_vega_lite_embed_csp_is_the_same_as_the_panel(monkeypatch, tmp_path):
    """vega-lite is the one inline kind with an explicit CSP meta; prove it's unconditioned by
    placement (so embed == panel) and still carries its full lockdown."""
    art = _load(monkeypatch, tmp_path)
    vega = _js_function(_js(art), "vegaDoc")
    # "embed" here is the vega-embed library; the placement global is EMBED — it must never reach
    # the chart's CSP, so embed and panel get the byte-identical lockdown.
    assert "EMBED" not in vega, "the chart CSP is built once, regardless of placement"
    assert vega.count("csp=") == 1  # one CSP string, not a per-placement fork
    for directive in (
        "default-src 'none'",
        "script-src 'nonce-",
        "connect-src 'none'",
        "worker-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
    ):
        assert directive in vega, directive
    assert "unsafe-eval" not in vega and "allow-same-origin" not in art._SHELL_HTML


def test_the_frame_sandbox_is_shared_and_opaque(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    # One #frame element, one sandbox, used by both placements — never same-origin.
    assert art._SHELL_HTML.count('<iframe id="frame"') == 1
    assert 'sandbox="allow-scripts allow-pointer-lock"' in art._SHELL_HTML
    assert "allow-same-origin" not in art._SHELL_HTML


# ── r2: an unknown id or version is an inert "unavailable" state ─────────────────


def test_version_resolution_handles_unknown_and_out_of_range(monkeypatch, tmp_path):
    """embedLocate resolves id + LIFETIME version to a kept position, or null — which the shell
    renders as the inert unavailable state (never an error)."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    harness = (
        _js_function(js, "total")
        + _js_function(js, "embedLocate")
        + r"""
// 3 kept versions of a lifetime 5 (oldest two trimmed at the cap): oldest kept = lifetime 3.
var arts=[{id:"a1", version_count:5, versions:[{ts:1},{ts:2},{ts:3}]}];
function i(id,ver){ var r=embedLocate(arts,id,ver); return r?r.idx:null; }
console.log(JSON.stringify({
  unknownId: i("nope",1),
  latest:    i("a1",0),    // 0/absent → latest kept
  oldestKept:i("a1",3),
  mid:       i("a1",4),
  newest:    i("a1",5),
  trimmed:   i("a1",2),    // below the oldest kept → gone
  tooHigh:   i("a1",99),
  noArts:    (function(){ var r=embedLocate([],"a1",1); return r?r.idx:null; })()
}));
"""
    )
    out = _node(harness)
    assert out["unknownId"] is None and out["noArts"] is None
    assert out["latest"] == 2 and out["newest"] == 2 and out["oldestKept"] == 0 and out["mid"] == 1
    assert out["trimmed"] is None and out["tooHigh"] is None


def test_embed_params_parse_the_query(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    harness = (
        _js_function(_js(art), "embedParams")
        + r"""
function p(s){ return embedParams(s); }
console.log(JSON.stringify({
  none:    p("?foo=1"),
  bare:    p("?embed=abc"),            // no v → latest (0)
  versioned:p("?embed=abc&v=3"),
  encoded: p("?embed=a%20b&v=2"),
  blank:   p("?embed=&v=2"),           // empty id → not an embed
  badv:    p("?embed=x&v=0"),          // non-positive v → latest (0)
}));
"""
    )
    out = _node(harness)
    assert out["none"] is None and out["blank"] is None
    assert out["bare"] == {"id": "abc", "ver": 0}
    assert out["versioned"] == {"id": "abc", "ver": 3}
    assert out["encoded"] == {"id": "a b", "ver": 2}
    assert out["badv"] == {"id": "x", "ver": 0}


def test_unavailable_state_is_inert_markup(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    html, js = art._SHELL_HTML, _js(art)
    # A dedicated element, hidden by default (so the panel is untouched), shown by showUnavail().
    assert 'id="unavail"' in html
    assert "#unavail{display:none" in html
    unavail = _js_function(js, "showUnavail")
    assert 'getElementById("unavail")' in unavail
    assert 'removeAttribute("srcdoc")' in unavail  # blank the frame — inert, not an error page
    assert "throw" not in unavail
    # The boot settles on the unavailable state when the id/version never appears in the store.
    boot = _js_function(js, "embedBoot")
    assert "showUnavail()" in boot and "EMBED_TRIES" in boot


# ── r3: the height message is posted on content change ───────────────────────────


def test_frame_reports_content_height_and_can_shrink(monkeypatch, tmp_path):
    """The reporter self-starts (on injection, on load, and when the shell sends
    protoArtifact:measure) and posts the content BOX height on every ResizeObserver callback. It
    measures getBoundingClientRect height, NOT documentElement.scrollHeight — scrollHeight is
    floored at the viewport (the frame's own height), so measuring it made height grow-only and
    kept the overflow:hidden svg/mermaid frames squashed. An unchanged measure is deduped."""
    if not NODE:
        pytest.skip("node not on PATH")
    art = _load(monkeypatch, tmp_path)
    harness = (
        _assignment(_js(art), "HEIGHTJS")
        + r"""
const posts = [];
let roCb = null, observed = 0;
global.window = {
  parent: { postMessage: (m) => posts.push(m) },
  addEventListener: (t, f) => { if (t === "message") global.__msg = f; },
  ResizeObserver: function (cb) { roCb = cb; this.observe = () => { observed++; }; },
};
// documentElement.scrollHeight is pinned to a huge VIEWPORT-floored value the reporter must
// ignore; `dh`/`bh` are the real content box heights it reads instead (so it can shrink).
let dh = 300, bh = 300;
global.document = {
  documentElement: { scrollHeight: 9999, getBoundingClientRect: () => ({ height: dh }) },
  body: { get scrollHeight() { return bh; }, getBoundingClientRect: () => ({ height: bh }) },
};
eval(HEIGHTJS.replace(/^<script>/, "").replace(/<\/script>$/, ""));
const onInit = posts.length;                               // self-starts on injection
global.__msg({ data: { type: "protoArtifact:measure" } }); // same height → deduped, no new post
const afterSameMeasure = posts.length;
dh = 540; bh = 540; roCb();                                // content grew
dh = 150; bh = 150; roCb();                                // content shrank
console.log(JSON.stringify({ onInit, afterSameMeasure, observed, posts }));
"""
    )
    out = _node(harness)
    assert out["onInit"] == 1, "the reporter self-starts in embed frames"
    assert out["afterSameMeasure"] == 1, "an unchanged measure is deduped"
    assert out["observed"] >= 1
    # The first post is the content box height (300), never the 9999 viewport-floored scrollHeight.
    assert out["posts"][0] == {"type": "protoArtifact:height", "height": 300}
    heights = [p["height"] for p in out["posts"]]
    assert 540 in heights, "a taller content change is reported"
    assert out["posts"][-1] == {"type": "protoArtifact:height", "height": 150}, "shrinking content is reported too"


def test_height_reporter_rides_every_embed_frame_not_just_base(monkeypatch, tmp_path):
    """Finding (correctness): HEIGHTJS used to live in base(), which the script-free file cards
    (table/json/text/sheets) and the nonce-CSP decks/PDF/Word frames never call — so those embeds
    never reported a height. It now rides the embed path (embedSuffix), appended to the frame the
    shared builder produced, so EVERY embed kind reports."""
    js = _js(_load(monkeypatch, tmp_path))
    assert "HEIGHTJS" not in _js_function(js, "base"), "the reporter must not live in base() (misses non-base cards)"
    embed = _js_function(js, "renderEmbed")
    assert "embedSuffix(doc, embedFill(a, v))" in embed, "renderEmbed must append the reporter to the built frame"
    assert "HEIGHTJS" in _js_function(js, "embedSuffix")
    # Fill vs flow: the viewport/paged/scroll-box cards get a sized box, the rest size to content.
    fill = _js_function(js, "embedFill")
    assert '"svg"' in fill and '"mermaid"' in fill and "previewKind(" in fill


def test_embed_suffix_reuses_the_frames_csp_nonce(monkeypatch, tmp_path):
    """The appended reporter must carry the doc's own CSP nonce for the nonce-CSP kinds (vega /
    slides / PDF / Word), or the inline script is blocked and the frame never reports; the
    nonce-free kinds get a bare <script>. embedSuffix only APPENDS — it never rewrites the CSP
    meta, so the embed frame's CSP stays identical to the panel's."""
    js = _js(_load(monkeypatch, tmp_path))
    harness = (
        _assignment(js, "HEIGHTJS")
        + _assignment(js, "EMBED_FLOW_CSS")
        + _assignment(js, "EMBED_FILL_CSS")
        + _js_function(js, "embedSuffix")
        + r"""
const csp = '<!doctype html><meta http-equiv="Content-Security-Policy" '
  + 'content="default-src \'none\'; script-src \'nonce-ABC123\'; style-src \'unsafe-inline\'">body';
const plain = '<!doctype html><meta charset="utf-8">body';
const s1 = embedSuffix(csp, true), s2 = embedSuffix(plain, false);
console.log(JSON.stringify({
  cspNonced: s1.includes('<script nonce="ABC123">'),
  cspNoBareScript: !s1.includes('<script>'),
  cspFill: s1.includes('clamp('),
  plainBare: s2.includes('<script>'),
  plainNoNonce: !s2.includes('nonce='),
  plainFlow: s2.includes('height:auto'),
}));
"""
    )
    out = _node(harness)
    assert out["cspNonced"] and out["cspNoBareScript"] and out["cspFill"]
    assert out["plainBare"] and out["plainNoNonce"] and out["plainFlow"]


def test_shell_wakes_the_frame_and_relays_the_height(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    # The shell sends the wake message on frame load — embed only (the panel never does).
    assert 'postMessage({type:"protoArtifact:measure"}' in js
    # The frame's reported height is sized onto the frame and relayed UP to the console host.
    relay = _js_function(js, "embedHeight")
    assert "$frame.style.height=h" in relay and "embedPostParent(" in relay
    up = _js_function(js, "embedPostParent")
    assert "window.parent.postMessage(msg, origin)" in up
    # The frame→shell height message is only acted on in embed mode.
    assert 'if(m.type==="protoArtifact:height"){ if(EMBED) embedHeight(m.height); return; }' in js


# ── r4: the panel (non-embed) view is unchanged ──────────────────────────────────


def test_panel_boot_and_render_are_unchanged(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    boot = _js_function(js, "boot")
    # Embed takes a dedicated boot; the panel keeps its exact selection + polling boot.
    assert "if (EMBED) { embedBoot(); return; }" in boot
    assert "loadSel(); poll(); schedulePoll();" in boot
    # The reporter rides only the embed path (embedSuffix), so no panel frame carries it; the one
    # measure sender is embed-gated, so the panel never pokes a frame about its height.
    assert js.count('postMessage({type:"protoArtifact:measure"}') == 1
    assert "if(!EMBED) return;" in _js(art)  # the measure sender is embed-gated
    # The panel render path never consults placement, nor appends the embed-only suffix.
    assert "EMBED" not in _js_function(js, "render")
    assert "embedSuffix" not in _js_function(js, "render")


def test_view_route_serves_the_embed_query_without_a_server_change(monkeypatch, tmp_path):
    """The embed URL is just the view page with a query the browser keeps; the server returns the
    same static page (so _routes.py needs no change)."""
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    plain = c.get("/plugins/artifact/view")
    embed = c.get("/plugins/artifact/view?embed=abc123&v=2")
    assert embed.status_code == 200 and embed.text == plain.text
    # still PUBLIC, never under the gated /api prefix (the base-derivation contract).
    assert c.get("/api/plugins/artifact/view?embed=abc123&v=2").status_code == 404


def test_shell_js_has_no_premature_script_close(monkeypatch, tmp_path):
    """The height reporter rides a srcdoc <script>, so its close must be escaped."""
    art = _load(monkeypatch, tmp_path)
    assert "</script>" not in _js(art)
    assert "<\\/script>" in _assignment(_js(art), "HEIGHTJS")
    assert "</" not in _assignment(_js(art), "HEIGHTJS").replace("<\\/script>", "")
