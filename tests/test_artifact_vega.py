"""`vega-lite` chart artifacts (ADR 0116) — the kind, the vendored renderer and its route, the
frame's CSP + loader lockdown, console theming, and the ``artifact.show`` plugin service.

The in-frame controller (``artVega``) is run under ``node`` with a stub ``vegaEmbed`` where node
is on PATH, so the lockdown and the theme are checked as BEHAVIOUR (what options the chart is
actually embedded with), not just as strings; those cases skip cleanly without node. A real
browser render is the live check in the PR (screenshots, dark + light)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from tests.test_artifact_plugin import ROOT, _app, _arts, _load
from tests.test_artifact_slides import _js_function

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive

NODE = shutil.which("node")

VENDOR = ROOT / "vendor"
VEGA_MJS = VENDOR / "pl-design-vega.mjs"
# @protolabsai/design is installed under the console workspace (apps/web). The repo root is
# ROOT.parent.parent; DS resolution walks up from apps/web/src, so the pinned 0.12.0 copy is here.
DESIGN_PKG = ROOT.parent.parent / "apps" / "web" / "node_modules" / "@protolabsai" / "design"


def _run_node(js: str) -> subprocess.CompletedProcess:
    """Run a shell-frame harness under node. The script rides a temp FILE (``node <file>``), NOT
    ``node -e`` — with the DS theme functions (``_ds_theme_src``) prepended, the one-liner can top
    Windows' ~32 KB command-line limit (``[WinError 206] The filename or extension is too long``);
    a deeply-nested spec spilled past it on the Windows lane. A ``.cjs`` extension keeps node in
    the same CommonJS/sloppy mode ``node -e`` uses, so the harness bodies are unchanged."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "frame.cjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(js)
        return subprocess.run([NODE, path], capture_output=True, text=True, timeout=60)


def _ds_theme_src() -> str:
    """The two DS theme functions as the chart frame gets them — read from the vendored module
    with ``export`` stripped (exactly what ``buildVegaConfig.toString()`` yields in the shell), so
    the node harness runs ``artVega`` with them in scope the way ``vegaDoc`` wires them in."""
    mod = VEGA_MJS.read_text(encoding="utf-8")
    return _js_function(mod, "buildVegaConfig") + "\n" + _js_function(mod, "vegaTooltipTheme") + "\n"


SPEC = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "weekday", "type": "nominal", "sort": "-y"},
        "y": {"field": "revenue", "type": "quantitative"},
    },
    "data": {"values": [{"weekday": "Sat", "revenue": 1840.5}, {"weekday": "Tue", "revenue": 910.0}]},
}


# ── the kind: create / edit / refuse ────────────────────────────────────────────


def test_show_artifact_creates_a_vega_lite_chart(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "vega-lite", "code": json.dumps(SPEC), "title": "Best weekdays"})
    assert "Created vega-lite artifact" in out
    [a] = _arts(art)
    assert a["kind"] == "vega-lite" and a["title"] == "Best weekdays"
    assert json.loads(a["versions"][-1]["code"]) == SPEC
    assert "vega-lite" in art._ref._KINDS  # the chat chip accepts the kind too


@pytest.mark.parametrize(
    ("code", "needle"),
    [
        ("{not json", "isn't valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ('"bar"', "must be a JSON object"),
        (json.dumps({"mark": "bar", "data": {"url": "https://example.com/x.csv"}}), "INLINE data only"),
        # nested: a layer's own data, and a lookup's secondary source
        (json.dumps({"layer": [{"mark": "line", "data": {"url": "data/x.json"}}]}), "INLINE data only"),
        (
            json.dumps(
                {"mark": "bar", "transform": [{"lookup": "k", "from": {"data": {"url": "/etc/passwd"}, "key": "k"}}]}
            ),
            "INLINE data only",
        ),
    ],
)
def test_a_bad_spec_is_refused_with_a_reason_and_nothing_is_written(monkeypatch, tmp_path, code, needle):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "vega-lite", "code": code})
    assert needle in out
    assert _arts(art) == []


def test_a_url_outside_any_data_block_is_not_a_data_url(monkeypatch, tmp_path):
    """Only `url` INSIDE a data definition is a load; an href field or usermeta note isn't."""
    art = _load(monkeypatch, tmp_path)
    spec = dict(SPEC, usermeta={"url": "https://example.com"}, encoding=dict(SPEC["encoding"], href={"field": "url"}))
    assert "Created vega-lite artifact" in art.show_artifact.invoke({"kind": "vega-lite", "code": json.dumps(spec)})


def test_edits_that_break_the_spec_are_refused(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "vega-lite", "code": json.dumps(SPEC)})
    out = art.update_artifact.invoke({"old_string": '"mark": "bar"', "new_string": '"mark": bar'})
    assert "isn't valid JSON" in out
    out = art.rewrite_artifact.invoke({"code": json.dumps({"mark": "point", "data": {"url": "x.csv"}})})
    assert "INLINE data only" in out
    assert len(_arts(art)[0]["versions"]) == 1  # neither edit committed a version
    ok = art.update_artifact.invoke({"old_string": '"mark": "bar"', "new_string": '"mark": "line"'})
    assert "version 2" in ok


def test_other_kinds_are_not_json_checked(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert "Created html artifact" in art.show_artifact.invoke({"kind": "html", "code": "{not json"})


# ── the artifact.show plugin service ────────────────────────────────────────────


def test_show_service_creates_and_returns_a_result_dict(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    r = art.show_service(kind="vega-lite", code=json.dumps(SPEC), title="Weekdays")
    assert r["ok"] is True and r["version"] == 1
    assert r["id"] == _arts(art)[0]["id"]
    assert "Created vega-lite artifact" in r["message"]
    # The artifact-ref chip tail rides separately, for the caller to append LAST.
    assert r["ref"] and r["id"] in r["ref"] and r["ref"] not in r["message"]


def test_show_service_returns_refusals_as_data(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    r = art.show_service(kind="vega-lite", code='{"mark":"bar","data":{"url":"x"}}')
    assert r == {"ok": False, "id": "", "version": 0, "message": r["message"], "ref": ""}
    assert "INLINE data only" in r["message"]
    assert art.show_service(kind="gif", code="x")["ok"] is False
    assert _arts(art) == []


def test_register_offers_the_service_by_name(monkeypatch, tmp_path):
    from graph.plugins.testkit import FakeRegistry

    art = _load(monkeypatch, tmp_path)
    reg = FakeRegistry(plugin_id="artifact")
    art.register(reg)
    assert reg.services["artifact.show"] is art.show_service
    desc = reg.service_meta["artifact.show"]["description"]
    # The description states the real default (#4076 follow-up): inline, not the panel.
    assert "defaults to 'inline'" in desc and "placement='panel'" in desc


def test_show_service_really_defaults_to_inline(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    r = art.show_service(kind="html", code="<p>x</p>")
    assert r["ok"] and _arts(art)[0].get("placement") == "inline"
    r = art.show_service(kind="html", code="<p>y</p>", placement="panel")
    assert r["ok"] and "placement" not in _arts(art)[0]


# ── the vendored renderer + its route ───────────────────────────────────────────


def test_vega_libs_are_vendored_and_served_same_origin(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    for name, glob in (
        ("vega.min.js", b".vega="),
        ("vega-lite.min.js", b".vegaLite="),
        ("vega-embed.min.js", b".vegaEmbed="),
    ):
        assert name in art._VENDOR_FILES
        r = c.get(f"/plugins/artifact/vendor/{name}")
        assert r.status_code == 200, name
        assert "javascript" in r.headers["content-type"]
        assert r.headers.get("access-control-allow-origin") == "*"  # SRI from the opaque sandbox
        assert glob in r.content[:400], name  # the UMD global the frame reads
    notices = (ROOT / "vendor" / "vega.LICENSES.txt").read_text(encoding="utf-8")
    for needle in ("vega 6.4.0", "vega-lite 6.4.3", "vega-embed 7.3.0", "BSD-3-Clause", "Redistribution and use"):
        assert needle in notices, needle


def test_the_ds_theme_module_is_vendored_and_served_same_origin(monkeypatch, tmp_path):
    """The DS theme is vendored like the other libs and served same-origin with CORS (the shell
    imports it as a module). It is NOT in the SRI LIB map: the shell load is same-origin, not an
    opaque-sandbox subresource, so it carries no integrity hash."""
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    assert "pl-design-vega.mjs" in art._VENDOR_FILES
    r = TestClient(_app(art)).get("/plugins/artifact/vendor/pl-design-vega.mjs")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("access-control-allow-origin") == "*"
    body = r.text
    assert "export function buildVegaConfig(tok)" in body
    assert "export function vegaTooltipTheme(tok)" in body
    assert "@protolabsai/design@" in body  # the header names the package + version
    assert "pl-design-vega.mjs" not in art._SHELL_JS.split("var LIB")[1].split("}", 1)[0]  # no SRI pin


def test_the_vendored_ds_functions_are_injection_safe():
    """Both functions ride a srcdoc <script> (vegaDoc injects their source beside artVega), so
    their bodies — as .toString() yields them, JSDoc excluded — carry no script-close or
    comment-open sequence, the same contract artVega holds."""
    mod = VEGA_MJS.read_text(encoding="utf-8")
    for fn in ("buildVegaConfig", "vegaTooltipTheme"):
        body = _js_function(mod, fn)
        for bad in ("</", "<!", "*/", "<script"):
            assert bad not in body, (fn, bad)


def test_the_vendored_ds_theme_matches_the_installed_package():
    """Drift guard: the vendored buildVegaConfig / vegaTooltipTheme are a VERBATIM copy of
    @protolabsai/design's `/vega` entry. Where the package is installed (apps/web/node_modules),
    the two functions must be byte-identical to the package source, and the vendored header must
    name the installed version — a DS bump that touches the chart theme has to be re-vendored
    here. Skips cleanly on the web-less lanes that don't install the console deps."""
    src = DESIGN_PKG / "src" / "vega.js"
    if not src.exists():
        pytest.skip("@protolabsai/design not installed (apps/web/node_modules)")
    pkg = src.read_text(encoding="utf-8")
    vend = VEGA_MJS.read_text(encoding="utf-8")
    for fn in ("buildVegaConfig", "vegaTooltipTheme"):
        assert _js_function(vend, fn) == _js_function(pkg, fn), f"{fn} drifted from @protolabsai/design"
    version = json.loads((DESIGN_PKG / "package.json").read_text(encoding="utf-8"))["version"]
    assert f"@protolabsai/design@{version}" in vend, f"vendor header must name design@{version}"


def test_vega_lite_routes_to_its_own_document(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    src = _js_function(art._SHELL_JS, "srcdoc")
    assert 'if (kind === "vega-lite") return vegaDoc(code);' in src
    assert '"vega-lite": "vl.json"' in art._SHELL_JS  # the panel's code download extension


def test_the_frame_theme_is_the_ds_functions_not_a_hand_copy(monkeypatch, tmp_path):
    """r1: artVega carries no hand-rolled theme()/isDark(); the config is built by the DS
    buildVegaConfig(tok) and the tooltip by vegaTooltipTheme(tok). The embed's config rides a thin
    chartConfig(tok) adapter that calls buildVegaConfig and then preserves this shell's prior
    range.category gate (see test_a_partial_series_set_keeps_the_prior_category_gate) — it is NOT a
    hand-rolled theme: every colour/axis/legend value still comes from the DS function. vegaDoc
    injects both DS functions' source beside artVega, and the shell loads them from the vendored
    same-origin module (not a copy pasted back into shell.js)."""
    art = _load(monkeypatch, tmp_path)
    ctl = _js_function(art._SHELL_JS, "artVega")
    assert "function isDark" not in ctl and "function theme(" not in ctl
    assert "config:chartConfig(tok)" in ctl  # the adapter…
    assert "buildVegaConfig(t" in ctl  # …which delegates the whole config to the DS builder
    assert "tooltip:{theme:vegaTooltipTheme(tok)}" in ctl
    doc = _js_function(art._SHELL_JS, "vegaDoc")
    assert "buildVegaConfig.toString()" in doc and "vegaTooltipTheme.toString()" in doc
    assert 'import(ORIGIN + "/plugins/artifact/vendor/pl-design-vega.mjs")' in art._SHELL_JS


# ── the frame: CSP + lockdown ───────────────────────────────────────────────────


def test_the_chart_frame_runs_under_a_nonce_csp_with_no_network(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    src = _js_function(art._SHELL_JS, "vegaDoc")
    assert "Content-Security-Policy" in src
    for directive in (
        "default-src 'none'",
        "script-src 'nonce-",
        "connect-src 'none'",
        "img-src data: blob:",
        "font-src data:",
        "worker-src 'none'",
        "frame-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ):
        assert directive in src, directive
    # No eval: Vega's expressions run as the interpreter, so 'unsafe-eval' is never needed.
    assert "unsafe-eval" not in src and "script-src 'unsafe-inline'" not in src
    for lib in ("vega", "vegaLite", "vegaEmbed"):
        assert f'cdn("{lib}", nonce)' in src  # each lib carries the nonce AND its SRI
    # The injected base scripts (ask bridge, error boot) are nonced too, or the CSP blocks them.
    assert "replace(/<script>/g, '<script nonce=\"'+nonce+'\">')" in src
    assert "allow-same-origin" not in art._SHELL_HTML
    ctl = _js_function(art._SHELL_JS, "artVega")
    assert "</" not in ctl and "<!" not in ctl  # rides a srcdoc <script>


def test_the_chart_csp_admits_the_vendored_embed_stylesheet(monkeypatch, tmp_path):
    """Launch bug B3: base() links the vendored DS sheet (pl-embed.css) into every frame, but the
    chart CSP was ``style-src 'unsafe-inline'`` — the sheet was refused on every chart, its
    ``error`` event read as "Script error", and a chart that drew fine failed check_artifact.
    style-src now names exactly that one vendored file (no host-wide grant)."""
    art = _load(monkeypatch, tmp_path)
    src = _js_function(art._SHELL_JS, "vegaDoc")
    assert "style-src 'unsafe-inline' \"+ORIGIN+\"/plugins/artifact/vendor/pl-embed.css;" in src
    assert '/plugins/artifact/vendor/pl-embed.css' in _js_function(art._SHELL_JS, "base")


def test_the_error_boot_waits_for_the_embed_not_the_load_event(monkeypatch, tmp_path):
    """A chart draws after `load` and can still fail then — so `load` must not report OK."""
    art = _load(monkeypatch, tmp_path)
    assert 'W.__artKind!=="react"&&W.__artKind!=="vega-lite"' in art._SHELL_JS


def test_theme_tokens_carry_the_chart_palette(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = art._SHELL_JS
    # themeTokens() now delegates to the DS embed's themeTokenSnapshot, which captures
    # ALL --pl-* tokens (including chart-axis/-grid/-series etc.) from the live root;
    # the old hand-written THEME_TOKENS list is gone.
    tok_fn = _js_function(js, "themeTokens")
    assert "themeTokenSnapshot(document.documentElement)" in tok_fn
    # chartConfig still reads the series-colour range from the snapshot record.
    chart_fn = _js_function(js, "chartConfig")
    assert '"--pl-color-chart-series"' in chart_fn
    # every frame's live re-theme pushes the same set the chart was drawn with
    push = _js_function(js, "pushTheme")
    assert "var tokens=themeTokens()" in push and "tokens:tokens" in push


# ── the frame controller, run under node against a stub vega-embed ─────────────

_HARNESS = r"""
const calls = [], errs = [], listeners = [];
let oks = 0;
const parent = {};
global.window = {
  parent,
  __artErr: (m) => errs.push(String(m)),
  __artOk: () => { oks++; },
  addEventListener: (t, f) => { if (t === "message") listeners.push(f); },
  vega: { Warn: 2, logger: () => ({ level() { return 2; }, warn() {}, error() {}, info() {}, debug() {} }) },
  vegaLite: {},
  vegaEmbed: (el, spec, opts) => { calls.push({ el, spec, opts }); return Promise.resolve({ finalize() {} }); },
};
if (__NOVEGA__) delete window.vegaEmbed;
(__SRC__)(__CFG__);
(async () => {
  await new Promise((r) => setTimeout(r, 0));
  const out = { errs, oks, calls: [] };
  for (const c of calls) {
    const o = c.opts, rej = {};
    for (const m of ["load", "sanitize", "http", "file"]) {
      try { await o.loader[m]("https://example.com/x"); rej[m] = "resolved"; } catch (e) { rej[m] = e.message; }
    }
    out.calls.push({ el: c.el, spec: c.spec, mode: o.mode, ast: o.ast, actions: o.actions, renderer: o.renderer,
      config: o.config, tooltip: o.tooltip, loaderRejects: rej, logger: typeof (o.logger && o.logger.error) });
  }
  if (__RETHEME__) {
    listeners.forEach((f) => f({ source: {}, data: { type: "protoArtifact:theme", tokens: { "--pl-color-bg": "#000" } } }));
    listeners.forEach((f) => f({ source: parent, data: { type: "protoArtifact:theme", tokens: __CFG__.tokens } }));
    listeners.forEach((f) => f({ source: parent, data: { type: "protoArtifact:theme", tokens: __LIGHT__ } }));
    await new Promise((r) => setTimeout(r, 0));
    out.rethemed = calls.slice(1).map((c) => ({ bg: c.opts.config.background, tooltip: c.opts.tooltip }));
  }
  console.log(JSON.stringify(out));
})();
"""

DARK = {
    "--pl-color-bg": "#0d0f14",
    "--pl-color-fg": "#ededed",
    "--pl-color-fg-muted": "#9aa0aa",
    "--pl-color-chart-axis": "#5b6070",
    "--pl-color-chart-grid": "#23262e",
    "--pl-font-sans": "Inter, system-ui, sans-serif",
    **{f"--pl-color-chart-series{i}": f"#a{i}a{i}a{i}" for i in range(1, 9)},
}
LIGHT = {**DARK, "--pl-color-bg": "#ffffff", "--pl-color-fg": "#111111"}


def _run_frame(art, spec, *, tokens=DARK, retheme=False, novega=False) -> dict:
    if not NODE:
        pytest.skip("node not on PATH")
    src = _js_function(art._SHELL_JS, "artVega")
    code = spec if isinstance(spec, str) else json.dumps(spec)
    # artVega now calls the DS buildVegaConfig/vegaTooltipTheme, which vegaDoc injects beside it;
    # mirror that by prepending them (as global declarations) to the harness.
    js = _ds_theme_src() + (
        _HARNESS.replace("__SRC__", src)
        .replace("__CFG__", json.dumps({"spec": code, "tokens": tokens}))
        .replace("__LIGHT__", json.dumps(LIGHT))
        .replace("__RETHEME__", "true" if retheme else "false")
        .replace("__NOVEGA__", "true" if novega else "false")
    )
    out = _run_node(js)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_chart_is_embedded_locked_down(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _run_frame(art, SPEC)
    assert out["errs"] == [] and out["oks"] == 1
    [c] = out["calls"]
    assert c["el"] == "#vis" and c["mode"] == "vega-lite" and c["renderer"] == "svg"
    assert c["ast"] is True  # the CSP-safe expression interpreter, never eval
    assert c["actions"] is False  # no export/editor menu (the editor action opens a remote site)
    assert c["logger"] == "function"
    for m, why in c["loaderRejects"].items():
        assert why.startswith("a chart's data must be inline"), (m, why)
    # Single view: fills the panel width.
    assert c["spec"]["width"] == "container" and c["spec"]["autosize"]["type"] == "fit-x"
    assert c["spec"]["data"] == SPEC["data"]


def test_the_chart_is_themed_from_the_console_tokens(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    cfg = _run_frame(art, SPEC)["calls"][0]["config"]
    assert cfg["background"] == "#0d0f14"
    assert cfg["range"]["category"] == [f"#a{i}a{i}a{i}" for i in range(1, 9)]
    assert cfg["mark"]["color"] == "#a1a1a1"
    assert cfg["axis"]["gridColor"] == "#23262e" and cfg["axis"]["domainColor"] == "#5b6070"
    assert cfg["axis"]["labelColor"] == "#9aa0aa" and cfg["title"]["color"] == "#ededed"
    assert cfg["font"] == "Inter, system-ui, sans-serif"


def test_the_config_is_the_ds_builder_output(monkeypatch, tmp_path):
    """The frame's config is @protolabsai/design's buildVegaConfig(tok) — the expected visible
    changes over the old hand-rolled theme: legends at the bottom, horizontal, 4 columns with no
    symbol cap (8+ series wrap), wider axis/legend label limits, and the #571 fix — the
    sequential/heatmap/diverging ranges emitted as hex so a heat map stops using Vega's blues."""
    art = _load(monkeypatch, tmp_path)
    cfg = _run_frame(art, SPEC)["calls"][0]["config"]
    lg = cfg["legend"]
    assert lg["orient"] == "bottom" and lg["direction"] == "horizontal" and lg["columns"] == 4
    assert lg["symbolLimit"] == 0 and lg["labelLimit"] == 200
    assert cfg["axis"]["labelLimit"] == 120  # design 0.13.0: one budget for single and composed views
    rng = cfg["range"]
    assert rng["category"] == [f"#a{i}a{i}a{i}" for i in range(1, 9)]
    for k in ("ramp", "heatmap", "diverging"):
        assert rng[k] and all(re.fullmatch(r"#[0-9a-f]{6}", s) for s in rng[k]), (k, rng[k])
    assert rng["heatmap"] == rng["ramp"]  # the DS maps both to the one sequential ramp


def test_a_heatmap_spec_gets_hex_ranges_from_the_ds_config(monkeypatch, tmp_path):
    """r5: a heatmap (a `rect` mark with a quantitative colour) draws with the DS range.heatmap —
    every stop a `#rrggbb`, which is what d3-color can interpolate (it cannot parse oklch)."""
    art = _load(monkeypatch, tmp_path)
    heat = {
        "mark": "rect",
        "encoding": {
            "x": {"field": "day", "type": "ordinal"},
            "y": {"field": "hour", "type": "ordinal"},
            "color": {"field": "load", "type": "quantitative"},
        },
        "data": {"values": [{"day": "Mon", "hour": 9, "load": 3}, {"day": "Tue", "hour": 10, "load": 7}]},
    }
    cfg = _run_frame(art, heat)["calls"][0]["config"]
    for k in ("heatmap", "ramp", "diverging"):
        assert cfg["range"][k] and all(re.fullmatch(r"#[0-9a-f]{6}", s) for s in cfg["range"][k])


def test_a_theme_switch_redraws_in_the_new_palette(monkeypatch, tmp_path):
    """A message from anywhere but the parent is ignored; the on-load push of the SAME tokens
    doesn't redraw; a real switch does — with the light tooltip on a light ground."""
    art = _load(monkeypatch, tmp_path)
    out = _run_frame(art, SPEC, retheme=True)
    assert out["calls"][0]["tooltip"] == {"theme": "dark"}
    assert out["rethemed"] == [{"bg": "#ffffff", "tooltip": {"theme": "light"}}]


def test_missing_tokens_fall_back_to_a_usable_palette(monkeypatch, tmp_path):
    """With no tokens the builder still emits a full, usable config from its literal defaults: the
    background/mark fall to the accent, and the sequential/diverging ranges are emitted as hex — the
    DS always themes the ramp now (#571), so a heat map never falls back to Vega's default blues
    (the pre-DS theme emitted no `range` for a single colour). But with no series tokens defined the
    shell drops range.category (see test_a_partial_series_set_keeps_the_prior_category_gate), so a
    multi-series chart keeps Vega's own varied category palette rather than painting every series
    the one accent — the behaviour the old theme() had below two defined series."""
    art = _load(monkeypatch, tmp_path)
    cfg = _run_frame(art, SPEC, tokens={})["calls"][0]["config"]
    assert cfg["background"] == "#0a0a0c" and cfg["mark"]["color"] == "#9b87f2"
    assert "category" not in cfg["range"]  # no series tokens → Vega's default category scheme
    for k in ("ramp", "heatmap", "diverging"):
        assert all(re.fullmatch(r"#[0-9a-f]{6}", s) for s in cfg["range"][k]), (k, cfg["range"][k])


def test_a_partial_series_set_keeps_the_prior_category_gate(monkeypatch, tmp_path):
    """The shell preserves the category contract the old hand-rolled `theme()` had — NOT the DS
    builder's always-eight-slots default. buildVegaConfig backfills every undefined
    `--pl-color-chart-series*` with the accent and always sets `range.category`; left as-is, a
    custom theme that defines one or no series colours would paint every series of a multi-series
    chart the one accent (the #571 review's regression). chartConfig() restores the old gate: a
    categorical palette is themed only when TWO OR MORE series colours are defined, and then from
    exactly those defined colours (no accent padding). The shipped console themes carry all eight
    tokens, so they are unaffected; this only protects a custom theme that omits some."""
    art = _load(monkeypatch, tmp_path)
    base = {"--pl-color-bg": "#0d0f14", "--pl-color-accent": "#9b87f2"}

    # 2+ defined → category is exactly the defined colours, no accent backfill of the empty slots.
    two = dict(base, **{"--pl-color-chart-series1": "#111111", "--pl-color-chart-series2": "#222222"})
    cfg = _run_frame(art, SPEC, tokens=two)["calls"][0]["config"]
    assert cfg["range"]["category"] == ["#111111", "#222222"]

    # Exactly one defined → no range.category at all, so Vega keeps its own varied palette rather
    # than collapsing the chart to the single accent-padded colour.
    one = dict(base, **{"--pl-color-chart-series1": "#111111"})
    cfg = _run_frame(art, SPEC, tokens=one)["calls"][0]["config"]
    assert "category" not in cfg["range"]
    assert cfg["mark"]["color"] == "#111111"  # the one defined series still paints a single mark

    # None defined → no category either; the default-accent mark still renders a single-series chart.
    cfg = _run_frame(art, SPEC, tokens=base)["calls"][0]["config"]
    assert "category" not in cfg["range"] and cfg["mark"]["color"] == "#9b87f2"


def test_a_spec_cannot_re_option_the_embed(monkeypatch, tmp_path):
    """vega-embed merges a spec's usermeta.embedOptions OVER the caller's — a loader with
    network access included. The frame strips it before embedding."""
    art = _load(monkeypatch, tmp_path)
    spec = dict(SPEC, usermeta={"embedOptions": {"loader": {"baseURL": "https://evil"}, "actions": True}, "note": 1})
    [c] = _run_frame(art, spec)["calls"]
    assert c["spec"]["usermeta"] == {"note": 1}
    assert c["actions"] is False


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        ("{oops", "isn't valid JSON"),
        ("[1]", "is a JSON object"),
        ({"mark": "bar", "data": {"url": "https://example.com/d.csv"}}, "data.url is not loaded"),
        ({"layer": [{"mark": "line", "data": {"url": "x.json"}}]}, "data.url is not loaded"),
    ],
)
def test_bad_specs_fail_loudly_and_never_embed(monkeypatch, tmp_path, spec, needle):
    art = _load(monkeypatch, tmp_path)
    out = _run_frame(art, spec)
    assert out["calls"] == [] and out["oks"] == 0
    assert any(needle in e for e in out["errs"]), out["errs"]


def test_a_missing_renderer_is_named(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _run_frame(art, SPEC, novega=True)
    assert out["calls"] == [] and any("renderer didn't load" in e for e in out["errs"])


def test_facets_keep_their_own_sizing(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    spec = {"facet": {"field": "k"}, "spec": {"mark": "bar"}, "data": {"values": []}}
    [c] = _run_frame(art, spec)["calls"]
    assert "width" not in c["spec"] and "autosize" not in c["spec"]


def test_the_vendored_builds_are_the_pinned_upstream_versions():
    """The bytes say which release they are — so a bump that forgets the notices fails here."""
    heads = {n: (ROOT / "vendor" / n).read_bytes() for n in ("vega.min.js", "vega-lite.min.js", "vega-embed.min.js")}
    assert re.search(rb'version\s*=\s*"6\.4\.0"|"6\.4\.0"', heads["vega.min.js"])
    assert b"6.4.3" in heads["vega-lite.min.js"]
    assert b"7.3.0" in heads["vega-embed.min.js"]


# ── review fixes: url COLUMNS are data; hostile nesting is a clean refusal ───────

URL_ROWS = {
    "mark": "bar",
    "encoding": {"x": {"field": "url", "type": "nominal"}, "y": {"field": "hits", "type": "quantitative"}},
    "data": {"values": [{"url": "/home", "hits": 10}, {"url": "/pricing", "hits": 4}]},
}


def test_a_url_column_in_inline_rows_is_data_not_a_load(monkeypatch, tmp_path):
    """A table of page hits has a `url` column — data_chart inlines it under data.values."""
    art = _load(monkeypatch, tmp_path)
    assert "Created vega-lite artifact" in art.show_artifact.invoke({"kind": "vega-lite", "code": json.dumps(URL_ROWS)})
    named = {
        "mark": "point",
        "data": {"name": "t"},
        "datasets": {"t": [{"url": "https://example.com", "n": 1}]},
    }
    assert art.show_service(kind="vega-lite", code=json.dumps(named))["ok"] is True
    # …while a real load is still refused, inline rows or not
    both = dict(URL_ROWS, layer=[{"mark": "line", "data": {"url": "x.csv"}}])
    assert "INLINE data only" in art.show_artifact.invoke({"kind": "vega-lite", "code": json.dumps(both)})


def _nested(depth: int) -> str:
    return '{"mark": "bar", "config": ' + '{"a": ' * depth + "1" + "}" * depth + "}"


@pytest.mark.parametrize("depth", [100, 700, 5000])
def test_a_deeply_nested_spec_is_a_clean_refusal(monkeypatch, tmp_path, depth):
    """Past the walk's bound (700) and past json's own recursion limit (5000): never a raise."""
    art = _load(monkeypatch, tmp_path)
    r = art.show_service(kind="vega-lite", code=_nested(depth))
    assert r["ok"] is False and "nest" in r["message"], r["message"][:200]
    assert "nest" in art.show_artifact.invoke({"kind": "vega-lite", "code": _nested(depth)})
    assert _arts(art) == []


def test_a_wide_spec_past_the_node_budget_is_refused(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    spec = {"mark": "bar", "config": {"x": [[] for _ in range(art._tools._SPEC_MAX_NODES + 10)]}}  # under 512 KB
    r = art.show_service(kind="vega-lite", code=json.dumps(spec))
    assert r["ok"] is False and "nodes" in r["message"]


def test_the_frame_embeds_url_columns_and_refuses_deep_specs(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _run_frame(art, URL_ROWS)
    assert out["errs"] == [] and len(out["calls"]) == 1
    deep = _run_frame(art, _nested(700))
    assert deep["calls"] == [] and any("too deeply nested" in e for e in deep["errs"]), deep["errs"]


# ── #4076: a re-theme never suppresses or delays the render verdict ─────────────

_RACE = r"""
const calls = [], errs = [], listeners = [];
let oks = 0;
const parent = {};
global.window = {
  parent,
  __artErr: (m) => errs.push(String(m)),
  __artOk: () => { oks++; },
  addEventListener: (t, f) => { if (t === "message") listeners.push(f); },
  vega: { Warn: 2, logger: () => ({ level() { return 2; }, warn() {}, error() {}, info() {}, debug() {} }) },
  vegaLite: {},
  // Each embed stays PENDING until the test settles it — a real draw takes ~100 ms, and a theme
  // push can land inside it.
  vegaEmbed: (el, spec, opts) => new Promise((res, rej) => {
    calls.push({ bg: opts.config.background, res: () => res({ finalize() {} }), rej });
  }),
};
const tick = () => new Promise((r) => setTimeout(r, 0));
const theme = (bg) => listeners.forEach((f) => f({ source: parent, data: { type: "protoArtifact:theme",
  tokens: Object.assign({}, __CFG__.tokens, { "--pl-color-bg": bg }) } }));
(__SRC__)(__CFG__);
(async () => {
  const out = {};
  // Theme pushes land while the FIRST draw is still in flight (two of them, a storm).
  theme("#111111"); theme("#222222"); theme("#333333");
  await tick();
  out.inFlight = calls.length;              // no restart: still the one draw
  calls[0].res(); await tick();
  out.oksAfterFirst = oks;                  // the first draw's verdict, before any redraw settles
  out.calls = calls.length;                 // ONE coalesced redraw, in the newest palette
  out.redrawBg = calls[calls.length - 1].bg;
  calls[calls.length - 1].res(); await tick();
  out.oksAfterRedraw = oks; out.errs = errs;
  console.log(JSON.stringify(out));
})();
"""


def test_a_retheme_mid_draw_never_delays_the_render_verdict(monkeypatch, tmp_path):
    """The verdict for an inline chart is the FIRST draw's (``__artOk``, once-only in ERRBOOT). A
    theme push that lands while that draw is in flight used to RESTART it — the superseded draw
    finalized without reporting, so the verdict waited on the redraw, and a run of pushes could
    keep deferring it. Now a mid-draw re-theme is coalesced into one redraw after the in-flight
    draw settles, and every settled draw reports."""
    art = _load(monkeypatch, tmp_path)
    if not NODE:
        pytest.skip("node not on PATH")
    src = _js_function(art._SHELL_JS, "artVega")
    js = _ds_theme_src() + _RACE.replace("__SRC__", src).replace(
        "__CFG__", json.dumps({"spec": json.dumps(SPEC), "tokens": DARK})
    )
    out = _run_node(js)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["inFlight"] == 1, "a re-theme must not restart the in-flight draw"
    assert r["oksAfterFirst"] == 1, "the first successful draw reports at once"
    assert r["calls"] == 2 and r["redrawBg"] == "#333333", "pushes coalesce into one redraw, newest palette"
    assert r["errs"] == []


def test_a_failed_first_draw_reports_even_when_a_retheme_is_pending(monkeypatch, tmp_path):
    """A spec that fails to draw reports its failure from the first draw too, not only after a
    pending re-theme's redraw."""
    art = _load(monkeypatch, tmp_path)
    if not NODE:
        pytest.skip("node not on PATH")
    src = _js_function(art._SHELL_JS, "artVega")
    js = _ds_theme_src() + (
        _RACE.replace("__SRC__", src)
        .replace("__CFG__", json.dumps({"spec": json.dumps(SPEC), "tokens": DARK}))
        .replace("calls[0].res(); await tick();", "calls[0].rej(new Error('bad encoding')); await tick(); out.errsAfterFirst = errs.slice();")
    )
    out = _run_node(js)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["errsAfterFirst"] == ["⚠ bad encoding"] and r["oksAfterFirst"] == 0
