"""The artifact frame's srcdoc swaps (#4076): "Open in panel" left a blank or stale panel.

A chip's "Open in panel" mounts the panel, whose boot rendered the REMEMBERED selection and then,
~2ms later, the chip's deep-link re-targeted the frame. Chrome drops a srcdoc assigned while the
frame's previous srcdoc navigation is still committing, so the frame kept the first document (or
stayed on its blank initial page) under the right picker label until a reload. Two guards:

* every srcdoc swap goes through ``frameNavigator``: identical content never re-navigates, a loaded
  frame swaps in place at once, and a second swap on a never-loaded frame goes into a fresh
  element (Chrome only drops swaps on a frame's first navigation);
* a fresh poll folds a pending deep-link into the selection BEFORE its one render.

The navigator runs under ``node`` (skips cleanly without it); the browser check is
``apps/web/e2e/artifact-chip.spec.ts``."""

from __future__ import annotations

import re

import pytest

from tests.test_artifact_plugin import _load
from tests.test_artifact_slides import _js_function, _node

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive


def _harness(js: str, body: str) -> str:
    return (
        _js_function(js, "frameNavigator")
        + r"""
// A fake iframe: records its srcdoc assignments, its load listeners and its replacement.
var made = 0, replacedTo = [];
function Frame() {
  this.n = ++made; this.assigned = []; this.loads = []; this.attrs = { srcdoc: null };
}
Frame.prototype = {
  set srcdoc(v) { this.assigned.push(v); this.attrs.srcdoc = v; },
  addEventListener: function (t, f) { if (t === "load") this.loads.push(f); },
  cloneNode: function () { var c = new Frame(); c.attrs = Object.assign({}, this.attrs); return c; },
  removeAttribute: function (k) { this.attrs[k] = null; },
  replaceWith: function (n) { this.replacedBy = n; },
  load: function () { this.loads.forEach(function (f) { f(); }); },
};
var first = new Frame(), current = first, themed = 0;
var nav = frameNavigator(first, function (n) { replacedTo.push(n.n); current = n; });
nav.onLoad(function () { themed++; });
"""
        + body
    )


def test_a_second_swap_on_a_fresh_frame_goes_into_a_new_element(monkeypatch, tmp_path):
    """Chrome drops a srcdoc assigned while a never-loaded frame commits its first one, so the
    newest document goes into a fresh element at once — no waiting on the abandoned load."""
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _harness(
            art._SHELL_JS,
            r"""
nav.set("A");                       // boot renders the remembered selection
nav.set("B");                       // the chip's deep-link lands ~2ms later
var second = current;
second.load();                      // the new element loads B → listeners carried over
console.log(JSON.stringify({
  firstAssigned: first.assigned, replacedTo: replacedTo, secondAssigned: second.assigned,
  carriedSrcdoc: second.attrs.srcdoc === "B", themed: themed, replacedBy: first.replacedBy && first.replacedBy.n,
}));
""",
        )
    )
    assert out["firstAssigned"] == ["A"]
    assert out["replacedTo"] == [2] and out["replacedBy"] == 2
    assert out["secondAssigned"] == ["B"], "the newest document goes in immediately, not on a later load"
    assert out["themed"] == 1, "load listeners (theme push, embed measure) follow the new element"


def test_a_loaded_frame_swaps_in_place_and_identical_content_never_renavigates(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _harness(
            art._SHELL_JS,
            r"""
nav.set("A"); first.load();         // loaded once → swaps are reliable from here on
nav.set("A");                       // identical content: no navigation, state survives
nav.set("B"); nav.set("C");         // quick swaps on a loaded frame: in place, newest last
console.log(JSON.stringify({ assigned: first.assigned, replacedTo: replacedTo }));
""",
        )
    )
    assert out["assigned"] == ["A", "B", "C"]
    assert out["replacedTo"] == [], "a loaded frame is never swapped for a new element"


def test_clear_lets_the_same_document_navigate_again(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _harness(
            art._SHELL_JS,
            r"""
nav.set("A"); first.load(); nav.clear(); nav.set("A");
console.log(JSON.stringify({ assigned: first.assigned }));
""",
        )
    )
    assert out["assigned"] == ["A", "A"], "a blanked frame re-renders the same document when it comes back"


def test_every_srcdoc_swap_goes_through_the_navigator(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    nav = _js_function(js, "frameNavigator")
    # The only direct assignment to the artifact frame's srcdoc is inside the navigator.
    outside = js.replace(nav, "")
    assert not re.search(r"\$frame\.srcdoc\s*=", outside), "assign the artifact frame via frameNav.set()"
    assert "frameNav.set(" in _js_function(js, "render")
    assert "frameNav.set(" in _js_function(js, "renderEmbed")


def test_a_fresh_poll_resolves_the_deep_link_before_its_render(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    poll = _js_function(js, "poll")
    # On a 200 the pending select is folded into the selection first, so the poll renders ONCE —
    # never the remembered artifact followed by a mid-navigation re-target.
    take, render = poll.index("takePendingSel(my)"), poll.index("render();")
    assert take < render
    assert poll.count("render();") == 1
