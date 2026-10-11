"""ADR 0118 final regression, plugin side (#4076).

- An inline artifact's create/edit reply can't carry a render verdict (its chat frame mounts only
  after the reply reaches the console), so the reply names the ``check_artifact(<id>)`` call.
- ``check_artifact`` on an inline artifact waits the full window even when no PANEL has polled
  (after a reload nothing has, until the inline frame mounts).
- The text-only note asks for the artifact id and warns off describing interactive controls.
- The shell relays each render verdict once per (artifact, version, verdict).
- A vega-lite spec's ``$schema`` is dropped before drawing (the renderer is always v6).
- The skill sends documents to the panel and diagrams to a mermaid artifact, not a chat fence.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from tests.test_artifact_plugin import _client, _load
from tests.test_artifact_slides import _js_function, _node

TEXT_ONLY = {"session_id": "s1", "renders_ui": False}
CONSOLE = {"session_id": "s1", "renders_ui": True}
SKILL_MD = (
    Path(__file__).resolve().parent.parent / "plugins" / "artifact" / "skills" / "rendering-artifacts" / "SKILL.md"
)


def _aid(art) -> str:
    return art._read_store()["artifacts"][0]["id"]


# ── 1. inline create/edit replies point at check_artifact ───────────────────────────


def test_inline_create_and_edits_name_the_check_artifact_call(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._render_status._LAST_POLL_TS = 0
    out = art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>", "state": CONSOLE})
    aid = _aid(art)
    pointer = f"call check_artifact('{aid}') before telling the user it works"
    assert "chat frame mounts" in out and pointer in out
    out = art.update_artifact.invoke({"old_string": "a", "new_string": "b", "state": CONSOLE})
    assert pointer in out
    out = art.rewrite_artifact.invoke({"code": "<p>c</p>", "state": CONSOLE})
    assert pointer in out


def test_inline_reply_with_a_verdict_carries_the_verdict_not_the_pointer(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>"})
    aid = _aid(art)
    store = art._read_store()
    store["artifacts"][0]["versions"][0]["render"] = {"ok": True, "error": "", "ts": art._now()}
    art._write_store(store)
    art._render_status._LAST_POLL_TS = art._now()
    suffix = art._render_suffix(aid, 1)
    assert "rendered cleanly" in suffix and "check_artifact" not in suffix


def test_panel_create_gets_no_inline_pointer(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._render_status._LAST_POLL_TS = 0
    out = art.show_artifact.invoke({"kind": "markdown", "code": "# Hi", "state": CONSOLE})
    assert "chat frame mounts" not in out


def test_text_only_create_is_unchanged_by_the_inline_pointer(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>", "state": TEXT_ONLY})
    assert "don't call check_artifact" in out and "chat frame mounts" not in out


def test_show_service_inline_message_names_the_check(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._render_status._LAST_POLL_TS = 0
    r = art.show_service(kind="vega-lite", code='{"mark":"bar","data":{"values":[]}}')
    assert f"check_artifact('{r['id']}')" in r["message"]


# ── 2. check_artifact waits for an inline frame with no panel poll ──────────────────


def test_check_artifact_waits_for_an_inline_frame_without_a_panel_poll(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>", "state": CONSOLE})
    aid = _aid(art)
    art._render_status._LAST_POLL_TS = 0  # a reload: nothing has polled yet
    assert art._renderer_live() is False
    art._render_status._RENDER_WAIT_MS = 5000
    c = _client(art)

    def frame_mounts():
        time.sleep(0.4)
        c.post("/api/plugins/artifact/render-status", json={"id": aid, "version": 1, "ok": True})

    t = threading.Thread(target=frame_mounts)
    t.start()
    try:
        t0 = time.monotonic()
        out = art.check_artifact.invoke({"artifact_id": aid, "state": CONSOLE})
        waited = time.monotonic() - t0
    finally:
        t.join()
    assert "rendered cleanly" in out
    assert 0.3 <= waited < 4.5


def test_check_artifact_does_not_wait_for_a_closed_panel(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "markdown", "code": "# a", "state": CONSOLE})
    art._render_status._LAST_POLL_TS = 0
    art._render_status._RENDER_WAIT_MS = 5000
    t0 = time.monotonic()
    out = art.check_artifact.invoke({"state": CONSOLE})
    assert time.monotonic() - t0 < 1.0
    assert "Artifact panel may be closed" in out


def test_check_artifact_text_only_still_answers_at_once(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>", "state": TEXT_ONLY})
    art._render_status._RENDER_WAIT_MS = 5000
    t0 = time.monotonic()
    out = art.check_artifact.invoke({"state": TEXT_ONLY})
    assert time.monotonic() - t0 < 1.0 and "no viewer on this client" in out


# ── 4. the text-only note ───────────────────────────────────────────────────────────


def test_text_only_note_asks_for_the_id_and_warns_off_controls(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "html", "code": "<button>Ask</button>", "state": TEXT_ONLY})
    aid = _aid(art)
    assert aid in out
    assert "quote the artifact id" in out
    assert "interactive controls" in out and "can't click them" in out


# ── 7. render-status relay de-dup (shell) ───────────────────────────────────────────


def test_shell_relays_each_render_verdict_once(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = art._SHELL_JS
    fn = _js_function(js, "relayRenderOnce")
    relay = js[js.index('if(m.type==="protoArtifact:render")') :][:600]
    assert "relayRenderOnce(arts, relayedRenders" in relay
    src = (
        "var RENDER_SEEN_KEY='k:';var window={};" + fn + ";var mem={}, out=[];"
        "out.push(relayRenderOnce([], mem, 'a', 1, 10, true, ''));"  # first verdict → post
        "out.push(relayRenderOnce([], mem, 'a', 1, 10, true, ''));"  # identical again → skip
        "out.push(relayRenderOnce([], mem, 'a', 1, 10, false, 'boom'));"  # a different verdict → post
        "out.push(relayRenderOnce([], mem, 'a', 2, 11, true, ''));"  # the next version → post
        # a remount (fresh memory) whose store mirror already holds this verdict → skip
        "out.push(relayRenderOnce([{id:'b',versions:[{ts:5,render:{ok:true,error:''}}]}], {}, 'b', 1, 5, true, ''));"
        # …but a mirror holding a DIFFERENT verdict → post
        "out.push(relayRenderOnce([{id:'b',versions:[{ts:5,render:{ok:false,error:'x'}}]}], {}, 'b', 1, 5, true, ''));"
        "console.log(JSON.stringify(out));"
    )
    assert _node(src) == [True, False, True, True, False, True]


def test_shell_relay_dedup_survives_a_remount_via_session_storage(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    fn = _js_function(art._SHELL_JS, "relayRenderOnce")
    src = (
        "var RENDER_SEEN_KEY='k:';var bag={};"
        "var window={sessionStorage:{getItem:function(k){return k in bag?bag[k]:null;},"
        "setItem:function(k,v){bag[k]=String(v);}}};" + fn + ";"
        "var a=relayRenderOnce([], {}, 'a', 1, 10, true, '');"
        "var b=relayRenderOnce([], {}, 'a', 1, 10, true, '');"  # a NEW shell frame, same tab
        "console.log(JSON.stringify([a,b]));"
    )
    assert _node(src) == [True, False]


# ── 6. vega-lite $schema ────────────────────────────────────────────────────────────


def test_vega_prep_drops_the_schema_pin(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    prep = _js_function(_js_function(art._SHELL_JS, "artVega"), "prep")
    assert 'delete s["$schema"]' in prep
    src = "var spec={'$schema':'https://vega.github.io/schema/vega-lite/v5.json',mark:'bar'};" + prep
    src += ";console.log(JSON.stringify(prep()));"
    out = _node(src)
    assert "$schema" not in out and out["mark"] == "bar"
    assert "omit ``$schema``" in art.show_artifact.description


# ── 3 + 5. the skill ────────────────────────────────────────────────────────────────


def test_skill_routes_documents_to_the_panel_and_diagrams_to_mermaid():
    text = SKILL_MD.read_text(encoding="utf-8")
    head = text.split("---", 2)[1]
    assert "README" in head and 'show_artifact(kind="markdown")' in head
    ladder_text_row = next(line for line in text.splitlines() if line.startswith("| **Text**"))
    assert "mermaid" not in ladder_text_row  # chat text shows a mermaid fence as code, not a diagram
    assert 'show_artifact(kind="mermaid"' in text
    assert "Leave `$schema` out" in text
    assert "check_artifact(<id>)" in text


def test_show_artifact_docstring_covers_documents_and_diagrams(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    d = json.dumps(art.show_artifact.description)
    assert "README" in d and 'kind=\\"markdown\\"' in d
    assert 'kind=\\"mermaid\\"' in d
