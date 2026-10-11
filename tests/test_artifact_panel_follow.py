"""The Artifact panel's follow-newest, live re-theme and refusal reporting (#4076, ADR 0118).

* Follow-newest follows PANEL artifacts only (D2: an inline answer has no side effect on the
  panel). The store's ``current`` moves to every new artifact, inline answers and other sessions'
  writes included, so the panel follows ``headOf`` — the newest panel-placed artifact — and moves
  only when that head changes.
* A live theme switch flips the frame's ``color-scheme`` too (native controls, scrollbars), not
  only its ``--pl-*`` tokens, and the shell page's own scheme follows the console, not the OS.
* A ``send()``/``openLink()`` refusal the artifact didn't catch is ``preventDefault``-ed, so the
  browser doesn't report it as an uncaught rejection; a real error still surfaces.

The shell helpers run under ``node`` (skipping cleanly without it); the browser checks are
``apps/web/e2e/artifact-panel.spec.ts``."""

from __future__ import annotations

import json

import pytest

from tests.test_artifact_embed import _assignment
from tests.test_artifact_plugin import _load
from tests.test_artifact_slides import _js_function, _node

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive


def _art(aid: str, ts: int, inline: bool = False) -> dict:
    a = {"id": aid, "kind": "svg" if inline else "markdown", "versions": [{"code": "x", "ts": ts}]}
    if inline:
        a["placement"] = "inline"
    return a


def _follow(js: str, steps: list[dict]) -> list[dict]:
    """Run ``nextSel`` over a sequence of polls ({arts, current}); return each poll's state."""
    harness = (
        _js_function(js, "headOf")
        + "\n"
        + _js_function(js, "nextSel")
        + "\nvar steps = "
        + json.dumps(steps)
        + r""";
var s = {selId: null, selVer: null, followNewest: true, head: null}, out = [];
steps.forEach(function (p) {
  if (p.pick) { s.selId = p.pick; s.followNewest = !!p.follow; out.push(null); return; }
  s = nextSel(p.arts, p.current, s); out.push({selId: s.selId, follow: s.followNewest, head: s.head});
});
console.log(JSON.stringify(out));
"""
    )
    return _node(harness)


def test_an_inline_answer_never_moves_the_panel(monkeypatch, tmp_path):
    """The repro: a markdown doc opens the panel; an inline svg in the same chat (now `current`)
    used to yank the panel onto it. The panel stays on the doc; the next PANEL artifact is
    still followed."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    doc, svg, doc2 = _art("doc", 100), _art("svg", 200, inline=True), _art("doc2", 300)
    out = _follow(
        js,
        [
            {"arts": [doc], "current": "doc"},
            {"arts": [svg, doc], "current": "svg"},
            {"arts": [doc2, svg, doc], "current": "doc2"},
        ],
    )
    assert out[0] == {"selId": "doc", "follow": True, "head": "doc"}
    assert out[1] == {"selId": "doc", "follow": True, "head": "doc"}, "an inline answer leaves the panel alone"
    assert out[2] == {"selId": "doc2", "follow": True, "head": "doc2"}, "a new panel artifact is still followed"


def test_another_sessions_inline_artifact_never_takes_over_the_panel(monkeypatch, tmp_path):
    """Another session (e.g. /v1) makes an html answer — inline by default — while the panel
    shows the newest; it becomes the store's current but not the panel's."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    doc, api = _art("doc", 100), _art("api", 200, inline=True)
    out = _follow(js, [{"arts": [doc], "current": "doc"}, {"arts": [api, doc], "current": "api"}])
    assert out[1]["selId"] == "doc"


def test_an_inline_artifact_opened_in_the_panel_stays_until_a_panel_artifact_arrives(monkeypatch, tmp_path):
    """"Open in panel" on an inline chip lands that artifact with follow on (it IS the newest);
    later inline answers don't move it, the next panel artifact does."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    doc, svg, svg2, doc2 = _art("doc", 100), _art("svg", 200, True), _art("svg2", 300, True), _art("doc2", 400)
    out = _follow(
        js,
        [
            {"arts": [svg, doc], "current": "svg"},
            {"pick": "svg", "follow": True},
            {"arts": [svg, doc], "current": "svg"},
            {"arts": [svg2, svg, doc], "current": "svg2"},
            {"arts": [doc2, svg2, svg, doc], "current": "doc2"},
        ],
    )
    assert out[2]["selId"] == "svg" and out[3]["selId"] == "svg"
    assert out[4]["selId"] == "doc2"


def test_a_store_of_only_inline_answers_still_shows_one_without_chasing_the_next(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    a, b = _art("a", 100, True), _art("b", 200, True)
    out = _follow(js, [{"arts": [a], "current": "a"}, {"arts": [b, a], "current": "b"}])
    assert out[0] == {"selId": "a", "follow": True, "head": None}, "never blank over a non-empty store"
    assert out[1]["selId"] == "a"


def test_head_is_the_newest_panel_artifact_and_a_pinned_selection_survives(monkeypatch, tmp_path):
    """Pins sort first, so the head is picked by write time, not position; a pinned (not
    following) selection is untouched until it is deleted, which falls back to the head."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    pinned_old, newer, svg = _art("old", 100), _art("new", 200), _art("svg", 300, True)
    out = _follow(
        js,
        [
            {"arts": [pinned_old, svg, newer], "current": "svg"},
            {"pick": "old", "follow": False},
            {"arts": [pinned_old, _art("n2", 400), svg, newer], "current": "n2"},
            {"arts": [svg, newer], "current": "svg"},
        ],
    )
    assert out[0]["selId"] == "new"
    assert out[2] == {"selId": "old", "follow": False, "head": "n2"}
    assert out[3] == {"selId": "new", "follow": True, "head": "new"}


def test_poll_and_picker_use_the_panel_follow_rules(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    poll = _js_function(js, "poll")
    assert "nextSel(arts, curId," in poll
    assert "selId = curId" not in poll, "the poll must not chase the store's current"
    assert "followNewest=atNewest(selId)" in js and "followNewest=atNewest(a.id)" in js


# ── F3: a live theme switch flips color-scheme too ──────────────────────────────────


def test_live_retheme_updates_the_frames_color_scheme_in_its_base_rule(monkeypatch, tmp_path):
    """The SHIM writes the pushed scheme into base()'s own :root rule (not inline), so an
    author's own color-scheme still wins, as at first render."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    assert "'<style id=\"__artbase\">:root{" in _js_function(js, "base")
    push = _js_function(js, "pushTheme")
    assert 'bgIsDark(tokens["--pl-color-bg"])' in push and "scheme:scheme" in push
    harness = (
        _assignment(js, "SHIM")
        + r"""
const listeners = [], set = {}, rule = {};
global.window = global;
global.addEventListener = (t, f) => { if (t === "message") listeners.push(f); };
global.parent = { postMessage: () => {} };
global.document = {
  documentElement: { style: { setProperty: (k, v) => { set[k] = v; } } },
  getElementById: (id) => id === "__artbase"
    ? { sheet: { cssRules: [{ style: { setProperty: (k, v) => { rule[k] = v; } } }] } } : null,
};
eval(SHIM.replace(/^<script>/, "").replace(/<\\?\/script>$/, ""));
const fire = (d) => listeners.forEach((f) => f({ data: d }));
fire({ type: "protoArtifact:theme", tokens: { "--pl-color-bg": "#f6f7f9" }, scheme: "light" });
const first = Object.assign({}, rule);
fire({ type: "protoArtifact:theme", tokens: { "--pl-color-bg": "#0a0a0c" }, scheme: "dark" });
const second = Object.assign({}, rule);
fire({ type: "protoArtifact:theme", tokens: {}, scheme: "red;x:y" });
console.log(JSON.stringify({ first, second, last: rule, inline: set }));
"""
    )
    out = _node(harness)
    assert out["first"] == {"color-scheme": "light"} and out["second"] == {"color-scheme": "dark"}
    assert out["last"] == {"color-scheme": "dark"}, "only dark/light are accepted"
    assert "color-scheme" not in out["inline"], "never an inline style that beats the author's CSS"
    assert out["inline"]["--pl-color-bg"] == "#0a0a0c"


# ── F5: a refusal is never reported as an uncaught rejection ────────────────────────


def test_an_uncaught_refusal_is_prevent_defaulted_and_a_real_error_is_not(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    harness = (
        _assignment(js, "ERRBOOT")
        + r"""
const listeners = {}, appended = [];
global.addEventListener = (t, f) => { (listeners[t] = listeners[t] || []).push(f); };
global.window = global;
global.parent = { postMessage: () => {} };
console.info = () => {};
global.document = { getElementById: () => null, createElement: () => ({ style: {} }),
  body: { appendChild: (d) => appended.push(d) } };
eval(ERRBOOT.replace(/^<script>/, "").replace(/<\\?\/script>$/, ""));
const ev = (reason) => { const e = { reason, prevented: false, preventDefault() { this.prevented = true; } };
  listeners.unhandledrejection.forEach((f) => f(e)); return e.prevented; };
const r = new Error("The user dismissed this message"); r.protoArtifactRefusal = true;
console.log(JSON.stringify({ refusal: ev(r), real: ev(new Error("boom")), overlays: appended.length }));
"""
    )
    out = _node(harness)
    assert out == {"refusal": True, "real": False, "overlays": 1}


def test_the_panel_frame_does_not_list_pointer_lock_in_allow(monkeypatch, tmp_path):
    """Pointer lock is the ``allow-pointer-lock`` SANDBOX token; Chrome doesn't know a
    ``pointer-lock`` permissions-policy feature and warned "Unrecognized feature" on every load."""
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
    assert 'sandbox="allow-scripts allow-pointer-lock"' in html
    assert 'allow="pointer-lock"' not in html
