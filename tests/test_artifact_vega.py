"""`vega-lite` chart artifacts (ADR 0116) — the kind, the vendored renderer and its route, the
frame's CSP + loader lockdown, console theming, and the ``artifact.show`` plugin service.

The in-frame controller (``artVega``) is run under ``node`` with a stub ``vegaEmbed`` where node
is on PATH, so the lockdown and the theme are checked as BEHAVIOUR (what options the chart is
actually embedded with), not just as strings; those cases skip cleanly without node. A real
browser render is the live check in the PR (screenshots, dark + light)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests.test_artifact_plugin import ROOT, _app, _arts, _load
from tests.test_artifact_slides import _js_function

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive

NODE = shutil.which("node")

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
    assert reg.service_meta["artifact.show"]["description"]


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


def test_vega_lite_routes_to_its_own_document(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    src = _js_function(art._SHELL_JS, "srcdoc")
    assert 'if (kind === "vega-lite") return vegaDoc(code);' in src
    assert '"vega-lite": "vl.json"' in art._SHELL_JS  # the panel's code download extension


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


def test_the_error_boot_waits_for_the_embed_not_the_load_event(monkeypatch, tmp_path):
    """A chart draws after `load` and can still fail then — so `load` must not report OK."""
    art = _load(monkeypatch, tmp_path)
    assert 'W.__artKind!=="react"&&W.__artKind!=="vega-lite"' in art._SHELL_JS


def test_theme_tokens_carry_the_chart_palette(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = art._SHELL_JS
    block = js[js.index("var THEME_TOKENS=") : js.index("function themeTokens(")]
    for tok in ("--pl-color-chart-axis", "--pl-color-chart-grid", "--pl-color-fg-muted", "--pl-font-sans"):
        assert tok in block, tok
    assert '"--pl-color-chart-series"+_s' in block
    # every frame's live re-theme pushes the same set the chart was drawn with
    assert "tokens:themeTokens()" in _js_function(js, "pushTheme")


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
    js = (
        _HARNESS.replace("__SRC__", src)
        .replace("__CFG__", json.dumps({"spec": code, "tokens": tokens}))
        .replace("__LIGHT__", json.dumps(LIGHT))
        .replace("__RETHEME__", "true" if retheme else "false")
        .replace("__NOVEGA__", "true" if novega else "false")
    )
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
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


def test_a_theme_switch_redraws_in_the_new_palette(monkeypatch, tmp_path):
    """A message from anywhere but the parent is ignored; the on-load push of the SAME tokens
    doesn't redraw; a real switch does — with the light tooltip on a light ground."""
    art = _load(monkeypatch, tmp_path)
    out = _run_frame(art, SPEC, retheme=True)
    assert out["calls"][0]["tooltip"] == {"theme": "dark"}
    assert out["rethemed"] == [{"bg": "#ffffff", "tooltip": {"theme": "light"}}]


def test_missing_tokens_fall_back_to_a_usable_palette(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    cfg = _run_frame(art, SPEC, tokens={})["calls"][0]["config"]
    assert cfg["background"] == "#0a0a0c" and cfg["mark"]["color"] == "#9b87f2"
    assert "range" not in cfg  # one colour isn't a categorical palette — Vega's default stays


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
