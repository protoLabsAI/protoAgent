"""Chrome-less embed placement of the artifact shell (ADR 0118 D2).

``/plugins/artifact/view?embed=<id>&v=<version>`` renders exactly ONE artifact version with no
panel chrome, through the SAME ``srcdoc()`` frame builder the panel uses — so its nonce CSP,
vendored SRI LIB map, theme tokens, loader lockdown and render-verdict reporting are all
inherited, and there is no second frame builder. The embed frame reports its content height
(a dormant reporter woken by ``protoArtifact:measure``) and the shell relays it to the console
host. An unknown id/version is an inert "unavailable" state, never an error page.

The in-frame height reporter and the version-resolution helper are run under ``node`` where it's
on PATH, so they're checked as BEHAVIOUR, not just as strings; those cases skip cleanly without
node. A real browser render is the live check in the PR."""

from __future__ import annotations

import pytest

from tests.test_artifact_plugin import ROOT, _app, _load
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
    assert 'return htmlDoc(code, dsLink() + base(kind))' in src
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


def test_frame_reports_height_on_measure_and_resize(monkeypatch, tmp_path):
    """The reporter is DORMANT until the shell sends protoArtifact:measure, then posts the
    content height on the wake and on every ResizeObserver callback (content change)."""
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
global.document = { documentElement: { scrollHeight: 321, offsetHeight: 300 },
                    body: { scrollHeight: 321, offsetHeight: 300 } };
eval(HEIGHTJS.replace(/^<script>/, "").replace(/<\/script>$/, ""));
const dormant = posts.length;                                   // nothing before a measure
global.__msg({ data: { type: "protoArtifact:measure" } });      // wake it
const afterMeasure = posts.length;
global.document.documentElement.scrollHeight = 540;             // content grew
global.document.body.scrollHeight = 540;
if (roCb) roCb();                                               // the ResizeObserver fires
console.log(JSON.stringify({ dormant, afterMeasure, observed, posts }));
"""
    )
    out = _node(harness)
    assert out["dormant"] == 0, "the panel never asks, so the reporter must stay silent until measured"
    assert out["afterMeasure"] == 1 and out["observed"] >= 1
    assert out["posts"][0] == {"type": "protoArtifact:height", "height": 321}
    assert out["posts"][-1] == {"type": "protoArtifact:height", "height": 540}


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
    # The reporter is injected into every frame but is DORMANT (acts only on measure), and the
    # panel never sends measure outside the embed-gated load handler.
    assert js.count('postMessage({type:"protoArtifact:measure"}') == 1
    assert "if(!EMBED) return;" in _js(art)  # the measure sender is embed-gated
    # The panel render path never consults placement.
    assert "EMBED" not in _js_function(js, "render")


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
    """The dormant reporter rides a srcdoc <script>, so its close must be escaped."""
    art = _load(monkeypatch, tmp_path)
    assert "</script>" not in _js(art)
    assert "<\\/script>" in _assignment(_js(art), "HEIGHTJS")
    assert "</" not in _assignment(_js(art), "HEIGHTJS").replace("<\\/script>", "")


def test_changelog_fragment_is_well_formed():
    frag = ROOT.parent.parent / "changelog.d" / "4081.added.md"
    text = frag.read_text(encoding="utf-8")
    assert text.startswith("- **"), "a changelog fragment MUST begin with a top-level bullet"
    assert "#4081" in text
