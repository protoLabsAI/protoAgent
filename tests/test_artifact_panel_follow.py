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


def _art(aid: str, ts: int, inline: bool = False, chats: list[str] | None = None) -> dict:
    a = {"id": aid, "kind": "svg" if inline else "markdown", "versions": [{"code": "x", "ts": ts}]}
    if inline:
        a["placement"] = "inline"
    if chats is not None:
        a["chat_sessions"] = chats
    return a


def _follow(js: str, steps: list[dict]) -> list[dict]:
    """Run ``nextSel`` over a sequence of polls ({arts, current}); return each poll's state."""
    harness = (
        _js_function(js, "followable")
        + "\n"
        + _js_function(js, "headOf")
        + "\n"
        + _js_function(js, "nextSel")
        + "\nvar steps = "
        + json.dumps(steps)
        + r""";
var s = {selId: null, selVer: null, followNewest: true, head: null}, session = null, out = [];
steps.forEach(function (p) {
  if (p.pick) { s.selId = p.pick; s.followNewest = !!p.follow; out.push(null); return; }
  if ("session" in p) session = p.session;  // the shell's chatSession: set by protoArtifact:context
  s.session = session;
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


# ── #4076 follow-up: the panel belongs to ONE chat ─────────────────────────────────


def test_another_chats_panel_artifact_never_takes_over_the_panel(monkeypatch, tmp_path):
    """The repro: chat A's doc is on the panel; chat B (another tab) — or a /v1, A2A or ACP client,
    whose artifacts carry no ``chat_sessions`` — makes a PANEL artifact. It becomes the store's
    current, but the panel (context: chat A) stays on A's doc; A's next panel artifact is followed."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    doc = _art("doc", 100, chats=["chat-a"])
    other = _art("other", 200, chats=["chat-b"])
    api = _art("api", 300)  # /v1 / A2A / ACP / legacy: no chat recorded
    doc2 = _art("doc2", 400, chats=["chat-a"])
    out = _follow(
        js,
        [
            {"session": "chat-a", "arts": [doc], "current": "doc"},
            {"arts": [other, doc], "current": "other"},
            {"arts": [api, other, doc], "current": "api"},
            {"arts": [doc2, api, other, doc], "current": "doc2"},
        ],
    )
    assert out[0] == {"selId": "doc", "follow": True, "head": "doc"}
    assert out[1] == {"selId": "doc", "follow": True, "head": "doc"}, "another chat's panel artifact"
    assert out[2] == {"selId": "doc", "follow": True, "head": "doc"}, "an unstamped (/v1, A2A, legacy) one"
    assert out[3] == {"selId": "doc2", "follow": True, "head": "doc2"}, "this chat's next one is followed"


def test_a_tab_switch_follows_that_chats_newest_panel_artifact(monkeypatch, tmp_path):
    """Switching to chat B moves a following panel to B's newest panel artifact; a chat with none
    leaves it where it is; back to A shows A's again. A pinned selection never moves."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    a, b = _art("a", 100, chats=["chat-a"]), _art("b", 200, chats=["chat-b"])
    store = {"arts": [b, a], "current": "b"}
    out = _follow(
        js,
        [
            {"session": "chat-a", **store},
            {"session": "chat-b", **store},
            {"session": "chat-new", **store},
            {"session": "chat-a", **store},
            {"pick": "b", "follow": False},
            {"session": "chat-b", **store},
            {"session": "chat-a", **store},
        ],
    )
    assert [o and o["selId"] for o in out] == ["a", "b", "b", "a", None, "b", "b"]


def test_a_chat_that_touched_an_artifact_follows_it(monkeypatch, tmp_path):
    """``chat_sessions`` lists every chat that created OR revised the artifact (#4138), so chat B
    editing A's doc makes it B's to follow too."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    mine, shared = _art("mine", 100, chats=["chat-b"]), _art("shared", 200, chats=["chat-a", "chat-b"])
    out = _follow(
        js, [{"session": "chat-b", "arts": [mine], "current": "mine"}, {"arts": [shared, mine], "current": "shared"}]
    )
    assert out[1]["selId"] == "shared"


def test_no_context_keeps_the_old_follow_and_an_empty_chat_follows_nothing_new(monkeypatch, tmp_path):
    """No context (an older console, a standalone page) → every panel artifact is followable, as
    before. An empty session (no active chat) follows nothing — but still never sits blank."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    doc, api = _art("doc", 100), _art("api", 200)
    legacy = _follow(js, [{"arts": [doc], "current": "doc"}, {"arts": [api, doc], "current": "api"}])
    assert legacy[1]["selId"] == "api"
    empty = _follow(js, [{"session": "", "arts": [doc], "current": "doc"}, {"arts": [api, doc], "current": "api"}])
    assert empty[0] == {"selId": "doc", "follow": True, "head": None}, "never blank over a non-empty store"
    assert empty[1]["selId"] == "doc"


def test_an_explicit_open_in_panel_still_shows_another_chats_artifact(monkeypatch, tmp_path):
    """A deep link (``protoArtifact:select``) is not auto-follow: it shows the artifact whatever
    chat made it, and the next foreign artifact still doesn't move the panel off it."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    out = _poll_harness(
        js,
        """
chatSession = "chat-a";
function S(id, ts, chats) { var a = A(id, ts); if (chats) a.chat_sessions = chats; return a; }
var p = poll(); Q.shift()(ok({artifacts: [S("doc", 100, ["chat-a"])], current: "doc"})); await p;
var q = poll(); Q.shift()(ok({artifacts: [S("api", 200), S("doc", 100, ["chat-a"])], current: "api"})); await q;
if (selId !== "doc") throw new Error("followed a foreign artifact: " + selId);
select("api", 1);
var r = poll(); Q.shift()(ok({artifacts: [S("api2", 300), S("api", 200), S("doc", 100, ["chat-a"])], current: "api2"})); await r;
""",
    )
    assert out["selId"] == "api" and out["pending"] is None


def test_the_context_message_is_trusted_only_from_the_embedder(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    i = js.index('m.type!=="protoArtifact:context"')
    guard = js[i - 200 : i + 120]
    assert "fromEmbedder(e)" in guard and "EMBED" in guard


def _stamped(a: dict) -> dict:
    """``a`` after the render-status stamp — the write a mounted frame makes after a create."""
    b = json.loads(json.dumps(a))
    b["versions"][-1]["render"] = {"ok": True, "error": "", "ts": b["versions"][-1]["ts"] + 1}
    return b


def test_the_f2_sequence_never_moves_the_panel_at_any_store_write(monkeypatch, tmp_path):
    """The headed-Chrome F2 confirmation, replayed write by write: a panel doc; a same-chat inline
    svg; another session's (/v1) inline html; each one's render-status stamp. A poll can land after
    ANY of those writes, and none of them moves the panel off the doc; the next panel doc does."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    doc, tile, intr, doc2 = _art("doc", 100), _art("tile", 200, True), _art("intr", 300, True), _art("doc2", 400)
    writes = [
        {"arts": [doc], "current": "doc"},
        {"arts": [_stamped(doc)], "current": "doc"},
        {"arts": [tile, _stamped(doc)], "current": "tile"},
        {"arts": [_stamped(tile), _stamped(doc)], "current": "tile"},
        {"arts": [intr, _stamped(tile), _stamped(doc)], "current": "intr"},
        {"arts": [_stamped(intr), _stamped(tile), _stamped(doc)], "current": "intr"},
    ]
    # Every subsequence a poller could observe (it always sees the first write: the doc opened it).
    for mask in range(1 << (len(writes) - 1)):
        seen = [writes[0]] + [w for i, w in enumerate(writes[1:]) if mask >> i & 1]
        out = _follow(js, [*seen, {"arts": [doc2, *seen[-1]["arts"]], "current": "doc2"}])
        assert [o["selId"] for o in out[:-1]] == ["doc"] * len(seen), f"polled writes {mask:05b}"
        assert out[-1]["selId"] == "doc2"


def _poll_harness(js: str, script: str) -> dict:
    """Run the shell's real ``poll`` + deep-link resolution with a hand-driven fetch: each
    ``kit.apiFetch`` parks in ``Q`` until ``script`` answers it, in whatever order it likes."""
    fns = "\n".join(
        _js_function(js, n)
        for n in (
            "followable",
            "headOf",
            "nextSel",
            "applyFollow",
            "atNewest",
            "total",
            "takePendingSel",
            "applyPendingSel",
        )
    )
    return _node(
        r"""
var document = {hidden: false}, Q = [];
var kit = {apiFetch: function () { return new Promise(function (res) { Q.push(res); }); }};
var lastEtag = "", storeChangedAt = 0, arts = [], curId = null, selId = null, selVer = null;
var followNewest = true, panelHead = null, chatSession = null, editing = false, pendingSel = null, pollSeq = 0, appliedSeq = 0;
function pollOk() {} function pollFailed() {} function saveHead() {} function saveSel() {} function exitEdit() {}
function rebuildArtSelect() {} function render() {}
function A(id, ts) { return {id: id, kind: "markdown", versions: [{code: "x", ts: ts}]}; }
function ok(store) { return {status: 200, ok: true, headers: {get: function () { return "e" + store.artifacts.length; }},
  json: function () { return Promise.resolve(store); }}; }
var NOT_MODIFIED = {status: 304, ok: true, headers: {get: function () { return ""; }}};
function select(id, ver) { pendingSel = {id: id, ver: ver, after: pollSeq}; applyPendingSel(0); }
"""
        + fns
        + "\nasync "
        + _js_function(js, "poll")
        + "\n(async function () {\n"
        + script
        + "\nconsole.log(JSON.stringify({selId: selId, follow: followNewest, ids: arts.map(function (a) { return a.id; }),"
        + " pending: pendingSel && pendingSel.id}));\n})();"
    )


def test_an_older_poll_answering_last_never_rolls_the_panel_back(monkeypatch, tmp_path):
    """Two polls overlap (a deep-link or a tab coming back kicks one while the timer's is in
    flight) and the OLDER one's answer lands last: it must not roll the store mirror — and the
    panel — back to the artifact the newer answer already moved it off."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    out = _poll_harness(
        js,
        """
var s0 = {artifacts: [A("doc", 100)], current: "doc"};
var s1 = {artifacts: [A("doc2", 200), A("doc", 100)], current: "doc2"};
var p = poll(); Q.shift()(ok(s0)); await p;
var older = poll(), newer = poll();
var answerOlder = Q.shift(), answerNewer = Q.shift();
answerNewer(ok(s1)); await newer;
answerOlder(ok(s0)); await older;
""",
    )
    assert out["selId"] == "doc2" and out["ids"] == ["doc2", "doc"]


def test_a_deep_link_survives_a_poll_that_was_already_in_flight(monkeypatch, tmp_path):
    """The agent's new panel artifact is announced (``protoArtifact:select``) while a poll sent
    BEFORE the artifact existed is still in flight. Its answer can't contain the id, and must not
    drop the request as "deleted": the next poll resolves it — even over a pinned selection."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    for stale in ("NOT_MODIFIED", 'ok({artifacts: [A("doc", 100)], current: "doc"})'):
        out = _poll_harness(
            js,
            f"""
var p = poll(); Q.shift()(ok({{artifacts: [A("doc", 100)], current: "doc"}})); await p;
followNewest = false;  // the operator pinned the doc
var inflight = poll();
select("doc2", 1);
Q.shift()({stale}); await inflight;
var next = poll(); Q.shift()(ok({{artifacts: [A("doc2", 200), A("doc", 100)], current: "doc2"}})); await next;
""",
        )
        assert out == {"selId": "doc2", "follow": True, "ids": ["doc2", "doc"], "pending": None}, stale
    # A request still unresolved after a poll issued AFTER it arrived is dropped (deleted/evicted).
    out = _poll_harness(
        js,
        """
var p = poll(); Q.shift()(ok({artifacts: [A("doc", 100)], current: "doc"})); await p;
select("gone", 1);
var next = poll(); Q.shift()(NOT_MODIFIED); await next;
""",
    )
    assert out["pending"] is None and out["selId"] == "doc"


def test_poll_and_picker_use_the_panel_follow_rules(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    poll = _js_function(js, "poll")
    assert "applyFollow();" in poll
    assert "nextSel(arts, curId," in _js_function(js, "applyFollow")
    assert "selId = curId" not in poll, "the poll must not chase the store's current"
    assert "followNewest=atNewest(selId)" in js and "followNewest=atNewest(a.id)" in js


# ── F3: a live theme switch flips color-scheme too ──────────────────────────────────


def test_live_retheme_updates_the_frames_color_scheme_in_its_base_rule(monkeypatch, tmp_path):
    """The SHIM writes the pushed scheme into base()'s own :root rule (not inline), so an
    author's own color-scheme still wins, as at first render."""
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    assert '<style id="__artbase">' in _js_function(js, "base")
    push = _js_function(js, "pushTheme")
    assert 'bgIsDark(tokens["--pl-color-bg"])' in push and "scheme:scheme" in push
    harness = (
        # the shim's per-navigation nonce placeholder (#4143 review)
        'var NONCE_SLOT = "__protoArtifactNonceSlot__";\n'
        + _assignment(js, "SHIM")
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
