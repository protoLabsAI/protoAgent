"""The artifact frame's per-navigation capability (#4143 review).

The nested artifact frame is sandboxed WITHOUT ``allow-top-navigation`` (it can't navigate the
shell), but it CAN navigate itself — and a WindowProxy keeps its identity across navigation, so
the shell's ``e.source === $frame.contentWindow`` gate alone let a third-party page that replaced
the artifact relay ``send`` / ``openLink`` / ``ask``. Now:

* ``frameNavigator`` swaps the shim's ``NONCE_SLOT`` for a fresh 128-bit nonce on every srcdoc
  assignment (identical content still never re-navigates), and REVOKES it on any second load —
  the document navigated itself;
* the shim echoes the nonce on ask/send/openLink;
* the shell relays (or answers ``ask``) only for a matching nonce (``capabilityOk``).

Runs under ``node`` (skips cleanly without it); the browser check is
``apps/web/e2e/artifact-send.spec.ts``."""

from __future__ import annotations

import re

import pytest

from tests.test_artifact_plugin import _load
from tests.test_artifact_slides import _js_function, _node

pytestmark = pytest.mark.platform_sensitive


def _nav_harness(js: str, body: str) -> str:
    return (
        'var NONCE_SLOT = "__protoArtifactNonceSlot__";\n'
        + _js_function(js, "mintNonce")
        + "\n"
        + _js_function(js, "frameNavigator")
        + "\n"
        + _js_function(js, "capabilityOk")
        + r"""
function Frame() { this.assigned = []; this.loads = []; this.attrs = { srcdoc: null }; }
Frame.prototype = {
  set srcdoc(v) { this.assigned.push(v); this.attrs.srcdoc = v; },
  addEventListener: function (t, f) { if (t === "load") this.loads.push(f); },
  cloneNode: function () { var c = new Frame(); c.attrs = Object.assign({}, this.attrs); return c; },
  removeAttribute: function (k) { this.attrs[k] = null; },
  replaceWith: function (n) { this.replacedBy = n; },
  load: function () { this.loads.forEach(function (f) { f(); }); },
};
var frame = new Frame();
var nav = frameNavigator(frame, function (n) { frame = n; });
var DOC = '<script>var N="__protoArtifactNonceSlot__";</script><p>art</p>';
"""
        + body
    )


def test_each_assignment_bakes_a_fresh_nonce_into_the_srcdoc(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _nav_harness(
            art._SHELL_JS,
            r"""
nav.set(DOC); frame.load();
var first = nav.nonce(), firstDoc = frame.assigned[0];
nav.set(DOC);                                   // identical content: no re-navigation, same nonce
var same = nav.nonce();
nav.set(DOC + "<p>v2</p>"); frame.load();
console.log(JSON.stringify({
  first: first, same: same, second: nav.nonce(), assigned: frame.assigned.length,
  baked: firstDoc.indexOf(first) >= 0, slotLeft: firstDoc.indexOf("__protoArtifactNonceSlot__") >= 0,
}));
""",
        )
    )
    assert re.fullmatch(r"[0-9a-f]{32}", out["first"])
    assert out["baked"] and not out["slotLeft"], "the srcdoc carries the nonce, never the placeholder"
    assert out["same"] == out["first"] and out["assigned"] == 2
    assert out["second"] != out["first"], "every real navigation gets a fresh nonce"


def test_a_second_load_revokes_the_nonce(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _nav_harness(
            art._SHELL_JS,
            r"""
nav.set(DOC); frame.load();
var live = nav.nonce();
var okBefore = capabilityOk(nav.nonce(), { nonce: live });
frame.load();                                   // the document navigated itself (location.href = …)
var after = nav.nonce();
var okAfter = capabilityOk(nav.nonce(), { nonce: live });
nav.set(DOC + "<p>next</p>"); frame.load();     // the shell's next render is a fresh document again
console.log(JSON.stringify({ okBefore: okBefore, after: after, okAfter: okAfter, fresh: nav.nonce() }));
""",
        )
    )
    assert out["okBefore"] is True
    assert out["after"] is None and out["okAfter"] is False
    assert re.fullmatch(r"[0-9a-f]{32}", out["fresh"])


def test_capability_ok_refuses_missing_wrong_or_revoked(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _nav_harness(
            art._SHELL_JS,
            r"""
console.log(JSON.stringify([
  capabilityOk("abc", { nonce: "abc" }), capabilityOk("abc", {}), capabilityOk("abc", { nonce: "abd" }),
  capabilityOk(null, { nonce: null }), capabilityOk(null, { nonce: "abc" }), capabilityOk("abc", null),
]));
""",
        )
    )
    assert out == [True, False, False, False, False, False]


def test_the_shim_echoes_the_baked_nonce_on_ask_send_and_open_link(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    # Evaluate the real SHIM string (its declaration + the slot it depends on), bake a nonce in the
    # way frameNavigator does, then run the shim against a fake window and capture its posts.
    start, end = js.index("  var NONCE_SLOT ="), js.index("  // Design-system surface:")
    decl = js[start:end]
    out = _node(
        decl
        + r"""
var script = SHIM.split(NONCE_SLOT).join("feedface").replace(/^<script>/, "").replace(/<\/script>$/, "");
var posted = [], window = { addEventListener: function () {} }, parent = { postMessage: function (m) { posted.push(m); } };
var setTimeout = function () {};
new Function("window", "parent", "setTimeout", script)(window, parent, setTimeout);
window.protoArtifact.ask("q").catch(function () {});
window.protoArtifact.send("hi").catch(function () {});
window.protoArtifact.openLink("https://x.example").catch(function () {});
console.log(JSON.stringify(posted.map(function (m) { return [m.type, m.nonce]; })));
"""
    )
    assert out == [
        ["protoArtifact:ask", "feedface"],
        ["protoArtifact:send", "feedface"],
        ["protoArtifact:openLink", "feedface"],
    ]


def test_the_relay_checks_the_capability_before_send_open_link_and_ask(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    i_gate = js.index("if(!capabilityOk(frameNav.nonce(), m))")
    assert i_gate < js.index('if(m.type==="protoArtifact:send" || m.type==="protoArtifact:openLink"){ relayBridge(m)')
    assert i_gate < js.index('if(m.type!=="protoArtifact:ask") return;')
    gate_line = js[js.rindex("\n", 0, i_gate - 200) : i_gate]
    for kind in ("protoArtifact:send", "protoArtifact:openLink", "protoArtifact:ask"):
        assert kind in gate_line
