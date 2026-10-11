"""The artifact frame's srcdoc swaps (#4076): "Open in panel" left a blank or stale panel.

A chip's "Open in panel" mounts the panel, whose boot rendered the REMEMBERED selection and then,
~2ms later, the chip's deep-link re-targeted the frame. Chrome drops a srcdoc assigned while the
frame's previous srcdoc navigation is still committing, so the frame kept the first document (or
stayed on its blank initial page) under the right picker label until a reload. Two guards:

* every srcdoc swap goes through ``frameNavigator``, which holds a swap requested mid-navigation
  until that navigation's ``load`` and then assigns only the newest document;
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
        "var FRAME_NAV_MAX_MS = 3000;\n"
        + _js_function(js, "frameNavigator")
        + r"""
var assigned = [], onLoad = null, timers = [];
var frame = {
  set srcdoc(v) { assigned.push(v); },
  addEventListener: function (t, f) { if (t === "load") onLoad = f; },
};
function later(fn, ms) { timers.push({ fn: fn, ms: ms, live: true }); return timers.length; }
function cancel(id) { if (id && timers[id - 1]) timers[id - 1].live = false; }
var nav = frameNavigator(frame, later, cancel);
"""
        + body
    )


def test_a_swap_mid_navigation_waits_for_load_and_lands_the_newest(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _harness(
            art._SHELL_JS,
            r"""
nav.set("A");                 // boot renders the remembered selection
nav.set("B");                 // the chip's deep-link lands mid-navigation…
nav.set("C");                 // …and a newer one before A has loaded
var during = assigned.slice();
onLoad();                     // A commits → only the newest queued doc goes in
var afterFirst = assigned.slice();
onLoad();                     // C commits → nothing left to assign
nav.set("D");                 // an idle frame swaps at once
console.log(JSON.stringify({ during: during, afterFirst: afterFirst, final: assigned }));
""",
        )
    )
    assert out["during"] == ["A"], "a swap requested mid-navigation must not be assigned yet"
    assert out["afterFirst"] == ["A", "C"], "on load, only the NEWEST queued document is assigned"
    assert out["final"] == ["A", "C", "D"]


def test_a_navigation_that_never_loads_does_not_strand_the_queue(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _harness(
            art._SHELL_JS,
            r"""
nav.set("A"); nav.set("B");
var live = timers.filter(function (t) { return t.live; });
live[live.length - 1].fn();   // no load ever came → the backstop timer fires
console.log(JSON.stringify({ assigned: assigned, ms: live[live.length - 1].ms }));
""",
        )
    )
    assert out["assigned"] == ["A", "B"]
    assert out["ms"] == 3000


def test_clear_drops_a_queued_swap(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = _node(
        _harness(
            art._SHELL_JS,
            r"""
nav.set("A"); nav.set("B"); nav.clear(); onLoad();
console.log(JSON.stringify({ assigned: assigned }));
""",
        )
    )
    assert out["assigned"] == ["A"], "a blanked/emptied frame must not be refilled by a stale queued doc"


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
    take, render = poll.index("takePendingSel(true)"), poll.index("render();")
    assert take < render
    assert poll.count("render();") == 1
