"""Tests for the artifact plugin — the tool, the history store, the route split,
and the plugin-view contract (the regression guard for the /api-vs-/plugins mount
bug). Run with: pytest (needs fastapi + langchain_core, the host's deps).

Artifact is bundled into core under ``plugins/artifact/`` (protoAgent #1443), so
ROOT anchors there off the repo root rather than the test's parent dir."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "plugins" / "artifact"


def _load(monkeypatch, tmp_path):
    """Fresh package bound to a temp ARTIFACT_DIR so history is isolated per test.

    Loaded as a PACKAGE (submodule_search_locations, the way the host loader does)
    so the plugin's relative imports resolve. Prior runs' submodules are evicted
    from sys.modules first — a cached ``artifact_under_test._tools`` would carry
    mutable module state (nudge counters, poll stamps) across tests."""
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    for k in [k for k in sys.modules if k.startswith("artifact_under_test")]:
        del sys.modules[k]
    spec = importlib.util.spec_from_file_location(
        "artifact_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["artifact_under_test"] = mod
    spec.loader.exec_module(mod)
    # No real browser in tests → never block a tool on the async render verdict (#1458). The
    # render-feedback tests drive the store directly; _await_render still returns an already-
    # recorded result on its first (pre-sleep) check.
    mod._render_status._RENDER_WAIT_MS = 0
    return mod


# ── the tools (create / update / rewrite / list / delete + versioning) ──────────


def _arts(art):
    return art._read_store()["artifacts"]


def test_show_artifact_rejects_unknown_kind(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "gif", "code": "x"})
    assert "Unknown artifact kind" in out
    assert _arts(art) == []  # nothing persisted on rejection


@pytest.mark.parametrize("kind", ["html", "svg", "mermaid", "react", "markdown"])
def test_show_artifact_creates_a_v1_artifact(monkeypatch, tmp_path, kind):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": kind, "code": "<x/>", "title": "T"})
    assert "Created" in out
    a = _arts(art)[0]
    assert a["kind"] == kind and a["title"] == "T"
    assert len(a["versions"]) == 1 and a["versions"][0]["code"] == "<x/>"
    assert art._read_store()["current"] == a["id"]


def test_kind_is_normalized(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "  HTML ", "code": "x"})
    assert _arts(art)[0]["kind"] == "html"


def test_update_artifact_appends_a_version_via_string_replace(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<h1>Hello</h1>"})
    out = art.update_artifact.invoke({"old_string": "Hello", "new_string": "World"})
    assert "version 2" in out
    a = _arts(art)[0]
    assert len(a["versions"]) == 2
    assert a["versions"][-1]["code"] == "<h1>World</h1>"
    assert a["versions"][0]["code"] == "<h1>Hello</h1>"  # v1 preserved (no clobber)


def test_update_requires_exactly_one_match(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>x</p><p>x</p>"})
    out = art.update_artifact.invoke({"old_string": "x", "new_string": "y"})
    assert "matches 2 times" in out
    assert len(_arts(art)[0]["versions"]) == 1  # not applied
    miss = art.update_artifact.invoke({"old_string": "zzz", "new_string": "y"})
    assert "not found" in miss
    assert len(_arts(art)[0]["versions"]) == 1


def test_update_with_no_artifact_is_a_clean_message(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert "No artifact" in art.update_artifact.invoke({"old_string": "a", "new_string": "b"})


def test_rewrite_replaces_whole_source_keeps_kind(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "svg", "code": "<svg>1</svg>", "title": "old"})
    out = art.rewrite_artifact.invoke({"code": "<svg>2</svg>", "title": "new"})
    assert "version 2" in out
    a = _arts(art)[0]
    assert a["kind"] == "svg" and a["title"] == "new"
    assert a["versions"][-1]["code"] == "<svg>2</svg>"


def test_update_targets_by_id_and_touches_to_front(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "first"})
    first_id = _arts(art)[0]["id"]
    art.show_artifact.invoke({"kind": "html", "code": "second"})  # now front
    art.update_artifact.invoke({"old_string": "first", "new_string": "FIRST", "artifact_id": first_id})
    arts = _arts(art)
    assert arts[0]["id"] == first_id  # edited artifact moved to front
    assert arts[0]["versions"][-1]["code"] == "FIRST"


def test_list_artifacts_summarizes(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert "No artifacts yet" in art.list_artifacts.invoke({})
    art.show_artifact.invoke({"kind": "mermaid", "code": "graph", "title": "Flow"})
    out = art.list_artifacts.invoke({})
    assert "Flow" in out and "[mermaid]" in out and "current" in out


def test_get_artifact_returns_current_source(monkeypatch, tmp_path):
    """get_artifact returns the actual code (not just metadata) so an agent can take over
    an artifact it didn't create. Defaults to current; targets another by id; clean miss."""
    art = _load(monkeypatch, tmp_path)
    assert "No artifact to read" in art.get_artifact.invoke({})  # none yet

    art.show_artifact.invoke({"kind": "html", "code": "<h1>First</h1>", "title": "One"})
    first = _arts(art)[0]["id"]
    art.show_artifact.invoke({"kind": "svg", "code": "<svg>2</svg>", "title": "Two"})

    # Default → the current (most recent) artifact's source.
    cur = art.get_artifact.invoke({})
    assert "<svg>2</svg>" in cur and "[svg]" in cur and "Two" in cur

    # Targeted → the older one's source, even though it isn't current (the takeover path).
    older = art.get_artifact.invoke({"artifact_id": first})
    assert "<h1>First</h1>" in older and "One" in older

    # After an edit, returns the latest version's code.
    art.update_artifact.invoke({"old_string": "2", "new_string": "9", "artifact_id": _arts(art)[0]["id"]})
    assert "<svg>9</svg>" in art.get_artifact.invoke({})


def test_delete_artifact_removes_and_repoints_current(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "a"})
    keep = _arts(art)[0]["id"]
    art.show_artifact.invoke({"kind": "html", "code": "b"})
    drop = _arts(art)[0]["id"]
    out = art.delete_artifact.invoke({"artifact_id": drop})
    assert "Deleted" in out
    store = art._read_store()
    assert [a["id"] for a in store["artifacts"]] == [keep]
    assert store["current"] == keep  # current re-pointed off the deleted one
    assert "No artifact" in art.delete_artifact.invoke({"artifact_id": "nope"})


def test_versions_rotate_to_max(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", "3")
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "v0"})
    for i in range(1, 5):
        art.rewrite_artifact.invoke({"code": f"v{i}"})
    versions = _arts(art)[0]["versions"]
    assert len(versions) == 3 and versions[-1]["code"] == "v4"  # oldest trimmed, newest kept


def test_artifacts_rotate_to_max(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "3")
    art = _load(monkeypatch, tmp_path)
    for i in range(5):
        art.show_artifact.invoke({"kind": "svg", "code": f"<n>{i}</n>"})
    arts = _arts(art)
    assert len(arts) == 3 and arts[0]["versions"][0]["code"] == "<n>4</n>"


def test_oversize_artifact_is_rejected_not_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_CODE_KB", "1")
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "html", "code": "x" * 2048})
    assert "too large" in out.lower()
    assert _arts(art) == []


def test_state_survives_a_reload_same_dir(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>kept</p>"})
    art.update_artifact.invoke({"old_string": "kept", "new_string": "edited"})
    art2 = _load(monkeypatch, tmp_path)  # fresh module, same ARTIFACT_DIR
    a = art2._read_store()["artifacts"][0]
    assert len(a["versions"]) == 2 and a["versions"][-1]["code"] == "<p>edited</p>"


def test_legacy_flat_history_migrates_to_versioned(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    # a pre-0.6 file: {"items": [flat artifacts]}
    art._store_path().write_text(
        '{"items": [{"id": "old1", "kind": "svg", "code": "<x/>", "title": "Legacy", "ts": 5}]}',
        encoding="utf-8",
    )
    store = art._read_store()
    a = store["artifacts"][0]
    assert a["id"] == "old1" and a["title"] == "Legacy"
    assert len(a["versions"]) == 1 and a["versions"][0]["code"] == "<x/>"
    assert store["current"] == "old1"


def test_bad_history_env_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "not-a-number")
    art = _load(monkeypatch, tmp_path)  # must not raise at import
    assert art._max_history() == 20  # bad value → default, never crashes


def test_config_layer_precedence_env_then_ui_then_default(monkeypatch, tmp_path):
    """A knob reads: explicit ENV > the host's plugin config (Settings ▸ Plugins) >
    literal default — so the UI toggle works and an env var still overrides it."""
    art = _load(monkeypatch, tmp_path)

    # default (no env, no host config — _plugin_cfg() returns {} without a host).
    assert art._ask_enabled() is False
    assert art._max_history() == 20

    # host/UI config drives it (simulate Settings ▸ Plugins → artifact.ask_enabled).
    monkeypatch.setattr(art._config, "_plugin_cfg", lambda: {"ask_enabled": True, "history": 7})
    assert art._ask_enabled() is True
    assert art._max_history() == 7

    # an explicit env var OVERRIDES the UI config (headless / ACP escape hatch).
    monkeypatch.setenv("ARTIFACT_ASK_ENABLED", "0")  # env wins → off despite UI True
    monkeypatch.setenv("ARTIFACT_HISTORY", "3")
    assert art._ask_enabled() is False
    assert art._max_history() == 3


def test_manifest_exposes_all_settings_fields(monkeypatch, tmp_path):
    import yaml

    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    by_key = {f["key"]: f for f in m.get("settings", [])}
    # every operator knob is a Settings ▸ Plugins field, with the right type.
    assert by_key["ask_enabled"]["type"] == "bool"
    assert by_key["ask_system"]["type"] == "string"
    for num in (
        "ask_max_chars",
        "history",
        "max_versions",
        "max_pinned",
        "max_code_kb",
        "max_blob_kb",
        "max_preview_kb",
    ):
        assert by_key[num]["type"] == "number", f"{num} should be a number field"
    # every settings key has a declared default in config:.
    assert set(by_key) <= set(m["config"])
    assert m["config"]["ask_enabled"] is False  # default off


def test_corrupt_store_file_reads_as_empty(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._store_path().write_text("{not json", encoding="utf-8")
    assert art._read_store() == {
        "artifacts": [],
        "current": None,
    }  # tolerated, not a 500


def test_instance_scoping_isolates_state(monkeypatch, tmp_path):
    # _store_path() reads PROTOAGENT_INSTANCE live, so a scoped instance routes
    # to its own subdir — no module reload needed.
    art = _load(monkeypatch, tmp_path)  # host (no instance)
    art.show_artifact.invoke({"kind": "svg", "code": "host"})
    assert _arts(art)[0]["versions"][0]["code"] == "host"
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    assert "roxy" in str(art._store_path())
    assert _arts(art) == []  # the roxy instance has its own (empty) state


# ── the full-body-write nudge (#2257) ──────────────────────────────────────────


def _tmp_file(tmp_path, name="doc.txt", content=b"hello"):
    f = tmp_path / name
    f.write_bytes(content)
    return str(f)


def _saved_id(out: str) -> str:
    return out.split("Saved file artifact ")[1].split(" ")[0]


def test_repeated_file_saves_draw_a_nudge(monkeypatch, tmp_path):
    # Field case: 11 saves of the same artifact in one turn. The third full-body
    # write inside the window carries the batching nudge; the turn never breaks.
    art = _load(monkeypatch, tmp_path)
    p = _tmp_file(tmp_path)
    out = art.save_file_artifact.invoke({"path": p})
    art_id = _saved_id(out)
    assert "NOTE:" not in out
    assert "NOTE:" not in art.save_file_artifact.invoke({"path": p, "artifact_id": art_id})
    out3 = art.save_file_artifact.invoke({"path": p, "artifact_id": art_id})
    assert "full-body write #3" in out3 and "Batch" in out3


def test_rewrites_count_but_targeted_updates_never_nudge(monkeypatch, tmp_path):
    # update_artifact is the cheap path we nudge TOWARD — it must stay exempt.
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<a>1</a>", "title": "T"})
    for old, new in [("1", "2"), ("2", "3"), ("3", "4"), ("4", "5")]:
        out = art.update_artifact.invoke({"old_string": old, "new_string": new})
        assert "NOTE:" not in out
    assert "NOTE:" not in art.rewrite_artifact.invoke({"code": "<b>x</b>"})
    assert "NOTE:" not in art.rewrite_artifact.invoke({"code": "<b>y</b>"})
    out3 = art.rewrite_artifact.invoke({"code": "<b>z</b>"})
    assert "full-body write #3" in out3


def test_nudge_window_expires(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    p = _tmp_file(tmp_path)
    art_id = _saved_id(art.save_file_artifact.invoke({"path": p}))
    art.save_file_artifact.invoke({"path": p, "artifact_id": art_id})
    real_now = art._now
    monkeypatch.setattr(art._store, "_now", lambda: real_now() + art._SAVE_NUDGE_WINDOW_MS + 1)
    # Old stamps aged out — the counter restarts instead of nagging forever.
    assert "NOTE:" not in art.save_file_artifact.invoke({"path": p, "artifact_id": art_id})


def test_tool_descriptions_teach_compose_once(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert "save ONCE" in art.save_file_artifact.description
    assert "batch your changes into one rewrite" in art.rewrite_artifact.description


# ── the routes (the split + gating contract) ───────────────────────────────────


def _app(art):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(art._build_view_router(), prefix="/plugins/artifact")
    app.include_router(art._build_data_router(), prefix="/api/plugins/artifact")
    return app


def test_view_page_served_on_the_PUBLIC_prefix(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    # The PAGE is public /plugins/artifact/view (iframe-loadable, base-derivation safe)…
    assert c.get("/plugins/artifact/view").status_code == 200
    # …and is NOT under /api (where the base would resolve to "/api" and break the kit).
    assert c.get("/api/plugins/artifact/view").status_code == 404


def test_data_routes_on_the_gated_prefix(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    assert c.get("/api/plugins/artifact/history").json() == {
        "artifacts": [],
        "current": None,
    }
    assert c.get("/api/plugins/artifact/current").json()["version"] == 0
    art.show_artifact.invoke({"kind": "svg", "code": "<x/>", "title": "T"})
    art.update_artifact.invoke({"old_string": "<x/>", "new_string": "<y/>"})
    cur = c.get("/api/plugins/artifact/current").json()
    assert cur["code"] == "<y/>" and cur["version"] == 2  # latest version of the focused artifact
    hist = c.get("/api/plugins/artifact/history").json()
    assert len(hist["artifacts"]) == 1 and len(hist["artifacts"][0]["versions"]) == 2
    assert hist["current"] == hist["artifacts"][0]["id"]


def test_history_is_conditional_etag_304(monkeypatch, tmp_path):
    """#2256: the panel polls /history continuously — an unchanged store must answer
    with an empty 304 (matched If-None-Match), and any mutation must rotate the tag."""
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))

    r1 = c.get("/api/plugins/artifact/history")
    etag = r1.headers.get("etag")
    assert r1.status_code == 200 and etag

    r2 = c.get("/api/plugins/artifact/history", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers.get("etag") == etag
    assert not r2.content  # 304 carries no body — the panel skips all work

    art.show_artifact.invoke({"kind": "svg", "code": "<x/>", "title": "T"})
    r3 = c.get("/api/plugins/artifact/history", headers={"If-None-Match": etag})
    assert r3.status_code == 200  # store changed → old tag no longer matches
    assert r3.headers.get("etag") and r3.headers.get("etag") != etag
    assert len(r3.json()["artifacts"]) == 1


def test_shell_polls_adaptively_with_etag(monkeypatch, tmp_path):
    """The shell page's poll loop (#2256): conditional fetch, 304 short-circuit,
    idle backoff constants, and no flat setInterval driver."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    assert "If-None-Match" in html
    assert "304" in html
    assert "POLL_IDLE_MS" in html and "POLL_FAST_MS" in html
    assert "setInterval(poll" not in html  # the flat 1.5s driver is gone


def test_shell_surfaces_persistent_poll_failures(monkeypatch, tmp_path):
    """#2885: a broken poll endpoint (401, 504, dead store) must not masquerade as the
    empty state. The shell counts consecutive failures, drops an error strip naming the
    HTTP status after 3, and clears it on the next successful poll (200 OR 304). A
    single transient miss stays silent."""
    art = _load(monkeypatch, tmp_path)
    js = art._SHELL_JS
    # threshold-of-3 counter + the strip's message naming the status.
    assert "POLL_FAIL_LIMIT = 3" in js
    assert "pollFails >= POLL_FAIL_LIMIT" in js
    assert "Couldn't load artifacts: " in js and "retrying" in js
    # a non-2xx must COUNT AS A FAILURE, not parse ({"detail":…} → arts=[]) into the
    # empty-state lie — the exact bug this guards against.
    assert "if (!r.ok) { pollFailed(" in js
    # both success shapes reset the streak and remove the strip; 304 short-circuits first.
    assert "if (r.status === 304) { pollOk(); return; }" in js
    assert "pollFails=0" in js
    # network-level errors (fetch threw — no response) count too; the bare swallow is gone.
    assert 'pollFailed("")' in js
    assert "catch (e) { /* transient */ }" not in js
    # the strip is DS-tokened (danger text on a muted inset ground), not hardcoded chrome.
    assert 'el.id="pollerr"' in js
    assert "color:var(--pl-color-status-error" in js
    assert "background:var(--pl-color-bg-inset" in js


def test_shell_delete_failure_flashes_on_the_button(monkeypatch, tmp_path):
    """#2885: a failed DELETE must say so — flash "Delete failed" on the button (the
    download button's pattern) and keep the selection (the artifact still exists),
    instead of silently swallowing and resetting the panel."""
    art = _load(monkeypatch, tmp_path)
    js = art._SHELL_JS
    assert '$del.textContent="Delete failed"' in js
    assert "catch(e){}" not in js  # the silent swallow is gone (shell-wide)
    # a non-2xx delete response is a failure too, not just a thrown fetch — the delete
    # handler carries its own `if(!r.ok) throw 0` (pin it to the DELETE call, not the
    # download/save handlers' pre-existing ones).
    assert 'method:"DELETE"});\n      if(!r.ok) throw 0; }' in js
    # the failure path bails BEFORE the selection reset + repoll.
    assert js.index('"Delete failed"') < js.index("selId=null; selVer=null; followNewest=true; saveSel(); poll();")


def test_shell_download_acknowledges_a_started_download(monkeypatch, tmp_path):
    """A successful Download must give transient, accessible feedback once ``saveBlob`` has
    fired — for BOTH the generated-source path and the stored-file blob path. Because the
    browser owns the save and completion isn't observable, the copy says the download STARTED
    (never that a file reached disk), it restores the normal "Download" label after a flash,
    and it announces the outcome on an aria-live status region. The failure path is preserved:
    a non-2xx / thrown blob request still flashes a failure and shows NO success ack."""
    art = _load(monkeypatch, tmp_path)
    js = art._SHELL_JS

    # an aria-live status region carries the outcome to AT (a bare button-label swap isn't announced).
    assert 'id="dlstat"' in art._SHELL_HTML
    assert 'role="status"' in art._SHELL_HTML
    assert 'aria-live="polite"' in art._SHELL_HTML

    # success copy says STARTED — the started-not-saved wording the acceptance criteria require.
    assert 'dlFlash("Started","Download started")' in js
    # BOTH download paths acknowledge: the stored-file blob path and the generated-source path.
    assert js.count('dlFlash("Started"') == 2
    # the file-blob ack sits INSIDE the try, AFTER the 2xx guard + saveBlob — so a non-2xx skips it.
    assert js.index("if(!r.ok) throw 0;") < js.index("saveBlob(await r.blob()")
    assert js.index("saveBlob(await r.blob()") < js.index('dlFlash("Started"')
    # the generated-source path acks after its own saveBlob of the code blob.
    assert js.index("saveBlob(new Blob([v.code]") < js.rindex('dlFlash("Started"')

    # failure feedback preserved, shown on exactly the failing path, with no success ack there.
    assert 'dlFlash("Failed","Download failed")' in js
    assert js.count('dlFlash("Failed"') == 1
    assert "catch(e){ dlFlash(" in js  # the failure branch is the only place a failure flashes

    # both outcomes restore the normal Download label after the flash.
    assert '$dl.textContent="Download"' in js


def test_delete_route_removes_the_artifact(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    art.show_artifact.invoke({"kind": "html", "code": "x"})
    aid = art._read_store()["artifacts"][0]["id"]
    r = c.delete(f"/api/plugins/artifact/artifact/{aid}")
    assert r.status_code == 200 and r.json()["deleted"] == aid
    assert art._read_store()["artifacts"] == []
    assert c.delete("/api/plugins/artifact/artifact/nope").status_code == 404


def test_put_route_saves_a_user_edit_as_a_new_version(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    art.show_artifact.invoke({"kind": "html", "code": "<p>v1</p>"})
    aid = art._read_store()["artifacts"][0]["id"]
    r = c.put(f"/api/plugins/artifact/artifact/{aid}", json={"code": "<p>v2 by user</p>"})
    assert r.status_code == 200 and r.json()["version"] == 2
    a = art._read_store()["artifacts"][0]
    assert a["versions"][-1] == {
        **a["versions"][-1],
        "code": "<p>v2 by user</p>",
        "by": "user",
    }
    assert a["versions"][0]["code"] == "<p>v1</p>"  # agent's v1 preserved (no clobber)
    # unknown id → 404; oversize → 413.
    assert c.put("/api/plugins/artifact/artifact/nope", json={"code": "x"}).status_code == 404
    monkeypatch.setenv("ARTIFACT_MAX_CODE_KB", "1")
    art2 = _load(monkeypatch, tmp_path)
    c2 = TestClient(_app(art2))
    big = c2.put(f"/api/plugins/artifact/artifact/{aid}", json={"code": "x" * 2048})
    assert big.status_code == 413


def test_ask_route_is_opt_in_and_validates(monkeypatch, tmp_path):
    import sys
    import types

    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    # Disabled by default → 403 (letting artifact code call the LLM is opt-in).
    monkeypatch.delenv("ARTIFACT_ASK_ENABLED", raising=False)
    assert c.post("/api/plugins/artifact/ask", json={"prompt": "hi"}).status_code == 403

    # Enabled: stub graph.sdk.complete (the host SDK isn't importable in the test env).
    monkeypatch.setenv("ARTIFACT_ASK_ENABLED", "1")
    captured = {}

    async def _fake_complete(prompt, *, system=None, model_name=None):
        captured["prompt"], captured["system"] = prompt, system
        return "agent says hi"

    fake = types.ModuleType("graph.sdk")
    fake.complete = _fake_complete
    monkeypatch.setitem(sys.modules, "graph", types.ModuleType("graph"))
    monkeypatch.setitem(sys.modules, "graph.sdk", fake)
    monkeypatch.setenv("ARTIFACT_ASK_SYSTEM", "be terse")

    r = c.post("/api/plugins/artifact/ask", json={"prompt": "  ping  "})
    assert r.status_code == 200 and r.json()["text"] == "agent says hi"
    assert captured == {
        "prompt": "ping",
        "system": "be terse",
    }  # trimmed + system passed

    assert c.post("/api/plugins/artifact/ask", json={"prompt": ""}).status_code == 400
    monkeypatch.setenv("ARTIFACT_ASK_MAX_CHARS", "5")
    art2 = _load(monkeypatch, tmp_path)
    c2 = TestClient(_app(art2))
    assert c2.post("/api/plugins/artifact/ask", json={"prompt": "way too long"}).status_code == 413


def test_manifest_view_path_matches_the_served_public_route(monkeypatch, tmp_path):
    import yaml

    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    path = m["views"][0]["path"]
    assert path == "/plugins/artifact/view"  # public, NOT /api/plugins/…
    # And the base a view derives from this path is empty (host) — the bug guard.
    assert path.split("/plugins/")[0] == ""


# ── the shell page: four-rules / kit contract ──────────────────────────────────


def test_shell_page_is_four_rules_compliant(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    # rule 4 — the same-origin DS kit, base-prefixed by hand (loads before the kit).
    assert "/_ds/plugin-kit.css" in html
    assert "/_ds/plugin-kit.js" in html
    assert 'location.pathname.split("/plugins/")[0]' in html
    # ESM — dynamic import, never a classic <script src> (protoContent#224).
    assert 'import(window.__base + "/_ds/plugin-kit.js")' in html
    assert 'type="module"' in html
    # rules 2+3 — gated data via the kit's slug-aware authed fetch.
    assert 'apiFetch("/api/plugins/artifact/history"' in html  # conditional fetch adds an init arg (#2256)
    # nested artifact frame stays sandboxed with NO same-origin (the isolation model);
    # allow-pointer-lock lets game/canvas artifacts capture the pointer (protoAgent #1443).
    assert 'sandbox="allow-scripts allow-pointer-lock"' in html
    assert "allow-same-origin" not in html
    # The kit owns the protoagent:init THEME handshake — the page's OWN chrome must not
    # hand-roll a :root theme (hex survives only as `var(--pl-color-…, #fallback)` defaults).
    # NB base() DOES emit a `:root` token-carry, but only into the nested ARTIFACT frame —
    # that frame has no kit, so it legitimately receives the live token values; scope the
    # guard to the shell page's own <style> block.
    page_style = html[html.index("<style>") : html.index("</style>")]
    assert ":root{" not in page_style and ":root {" not in page_style
    assert "kit.initPluginView" in html  # kit owns theming, not a bespoke listener
    assert "applyTheme" not in html  # the pre-kit hand-rolled theme fn is gone


def test_edit_overlay_does_not_teardown_the_frame(monkeypatch, tmp_path):
    """Regression: toggling the in-panel editor must NOT hide + re-srcdoc the artifact
    frame. The editor is an opaque absolute overlay, so the frame stays laid out; the old
    code re-rendered on exit, which raced the reflow and made mermaid measure text at 0
    size (`transform: translate(undefined, NaN)` → a black panel the `lastRendered` cache
    never repainted). Keep the frame visible/sized the whole time."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    # the editor overlays the stage, so editing never needs to tear the frame down.
    assert "#editor{position:absolute;inset:0" in html
    # exitEdit must not force a re-render of the (un-changed) frame on exit — the
    # `…display="none"; lastRendered=""; render()` signature that caused the black panel.
    assert 'lastRendered=""; render()' not in html


def test_ask_bridge_is_wired(monkeypatch, tmp_path):
    """The window.protoArtifact.ask shim is injected into artifacts and the shell
    relays it to the gated /ask endpoint (the agent-callback bridge)."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    assert "window.protoArtifact" in html and "protoArtifact:ask" in html
    assert "protoArtifact:result" in html
    assert 'apiFetch("/api/plugins/artifact/ask"' in html
    # the shell only relays messages from its own artifact frame.
    assert "e.source!==$frame.contentWindow" in html


def test_graphic_kinds_get_a_crisp_fit_to_window_viewport(monkeypatch, tmp_path):
    """svg + mermaid render into a CRISP fit-to-window viewport (#1517): the <svg> scales as a
    VECTOR to fit the frame (max-width/height:100% !important — the !important beats mermaid's
    inline max-width:Npx). No CSS transform / raster layer, which pixelated on zoom-in in
    WKWebView; pan/zoom is intentionally traded away for crispness. Both kinds share the one
    `viewport(...)` wrapper."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    # the viewport container exists and fits the svg crisply as a vector.
    assert 'id="__vp"' in html
    assert "max-width:100% !important" in html and "max-height:100% !important" in html
    assert "width:auto !important" in html and "height:auto !important" in html
    # both graphic kinds route through the shared wrapper.
    assert "function viewport(inner)" in html
    # the old transform-driven raster pan/zoom scaffold + controls are GONE (they pixelated).
    assert 'id="__cv"' not in html
    assert 'id="__zi"' not in html and 'id="__zo"' not in html and 'id="__zr"' not in html
    assert "will-change" not in html and "transform-origin" not in html
    assert 'addEventListener("wheel"' not in html  # no scroll-zoom
    assert "__artFit" not in html  # no async re-fit — the fit is pure CSS now


def test_libs_are_vendored_same_origin_not_cdn(monkeypatch, tmp_path):
    """react/mermaid load from the same-origin vendor route — NO cdnjs (so artifacts
    work offline), every lib still SRI-pinned (sha512 of the vendored bytes)."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    assert "cdnjs.cloudflare.com" not in html  # no external CDN dependency
    assert "/plugins/artifact/vendor/" in html  # served same-origin
    # all four libs present, each with an integrity hash.
    for lib in (
        "mermaid.min.js",
        "react.production.min.js",
        "react-dom.production.min.js",
        "babel.min.js",
    ):
        assert lib in html
    assert html.count("sha512-") == 4 and 'integrity="' in html
    # crossorigin is REQUIRED even same-origin: the sandbox is an opaque origin, so
    # the lib load is cross-origin and SRI needs the CORS fetch to validate.
    assert 'crossorigin="anonymous"' in html


def test_vendored_files_exist_and_match_the_allowlist(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    vendor = ROOT / "vendor"
    for name in art._VENDOR_FILES:
        assert (vendor / name).exists(), f"vendor/{name} missing"
    # no stray files served that aren't on disk, no disk files unlisted (UMD .js + ESM .mjs).
    on_disk = {p.name for p in vendor.iterdir() if p.suffix in (".js", ".mjs")}
    assert on_disk == art._VENDOR_FILES


def test_vendor_route_serves_js_and_blocks_traversal(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    for name in ("react.production.min.js", "d3.mjs", "pl-ui.mjs", "marked.mjs"):
        r = c.get(f"/plugins/artifact/vendor/{name}")
        assert r.status_code == 200, name
        assert "javascript" in r.headers["content-type"]  # ESM must be served as JS
        assert "immutable" in r.headers.get("cache-control", "")
        assert (
            r.headers.get("access-control-allow-origin") == "*"
        )  # CORS — opaque-sandbox cross-origin fetch (module + SRI)
    # allowlist: an unlisted name / traversal attempt is a clean 404, not a file read.
    assert c.get("/plugins/artifact/vendor/secrets.env").status_code == 404
    assert c.get("/plugins/artifact/vendor/..%2f__init__.py").status_code == 404


# ── the new kinds: markdown + the react import map + the DS surface ──────────────


def test_react_kind_uses_import_map_and_module_babel(monkeypatch, tmp_path):
    """react artifacts compile as a MODULE (so `import` works) and ship a curated import
    map resolving bare specifiers to the same-origin vendored ESM modules."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    assert 'type="importmap"' in html
    assert 'data-type="module"' in html  # babel compiles to a module → top-level import ok
    # bare specifiers → vendored modules (incl. the React shims that share one React instance)
    for spec, file in (
        ('"react":', "react.shim.mjs"),
        ('"react-dom/client":', "react-dom-client.shim.mjs"),
        ('"@pl/ui":', "pl-ui.mjs"),
        ('"d3":', "d3.mjs"),
        ('"chart.js":', "chartjs.mjs"),
        ('"lucide":', "lucide.mjs"),
    ):
        assert spec in html and file in html, spec


def test_harness_guards_against_silent_blank(monkeypatch, tmp_path):
    """Hardening: the harness surfaces errors (global handlers + a lazy `__arterr` overlay via
    base(), so it covers every kind) and, for react, flags a component that's DEFINED but never
    mounted into #root — so a broken artifact shows WHY instead of a silent blank (the
    'looks stuck' failure mode)."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    # Universal error surfacing (base() → every artifact frame).
    assert "window.__artErr" in html
    assert 'addEventListener("error"' in html
    assert 'addEventListener("unhandledrejection"' in html
    assert '"__arterr"' in html  # the overlay element id
    # React no-mount guard: actionable message instead of a blank #root (now points at the
    # `App` auto-mount convention, since defining `App` is enough).
    assert "name your top-level component" in html
    assert "Nothing rendered into #root" in html


def test_markdown_kind_renders_via_marked(monkeypatch, tmp_path):
    """markdown artifacts render via the vendored `marked` ESM into #md; the source is
    base64'd into the module (no quote/newline/</script> escaping pitfalls)."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    assert 'import { marked } from "marked"' in html
    assert "marked.mjs" in html and 'id="md"' in html
    assert "atob(" in html and "btoa(" in html  # base64 round-trip of the source
    assert "language-mermaid" in html  # ```mermaid fences upgrade to live diagrams


def test_ds_kit_injected_into_artifacts(monkeypatch, tmp_path):
    """html/react/markdown artifacts link the same-origin DS plugin-kit stylesheet so the
    `.pl-*` classes + `--pl-*` tokens work inside the sandbox and match the console theme."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    assert "/_ds/plugin-kit.css" in html and "function dsLink()" in html
    # the live theme's key tokens are carried into the nested (no-stylesheet-access) frame.
    assert "--pl-color-accent:" in html


def test_pl_ui_wrapper_module_is_vendored_and_sound():
    """The authored @pl/ui module imports React via the bare specifier (→ the shim → one
    shared instance) and wraps the DS classes; the Icon component is lucide-backed."""
    src = (ROOT / "vendor" / "pl-ui.mjs").read_text()
    assert 'from "react"' in src and 'from "lucide"' in src
    for name in ("Button", "Card", "Stat", "Alert", "Icon"):
        assert f"export function {name}" in src, name
    assert "pl-btn" in src and "pl-card" in src  # mirrors the DS class contracts
    # the React shim re-exports the UMD global (single instance).
    shim = (ROOT / "vendor" / "react.shim.mjs").read_text()
    assert "window.React" in shim and "export default" in shim


def test_no_premature_script_close_in_shell(monkeypatch, tmp_path):
    """Regression: a literal ``</script>`` anywhere in the shell's module script — even in a
    JS comment or string — closes that ``<script type=module>`` EARLY (the HTML parser doesn't
    know JS syntax), breaking boot (empty picker / blank frame / a stray invalid import map).
    Every script the shell INJECTS into an artifact must escape its close as ``<\\/script>``;
    only the shell's own two ``<script>`` blocks may carry a real close."""
    art = _load(monkeypatch, tmp_path)
    assert art._SHELL_HTML.count("</script>") == 2, (
        "exactly the slug-base inline close + the shell.js module tag's close; an extra "
        "literal </script> would close a script early"
    )
    # shell.js is a REAL .js file now — the outer page can't be truncated by it, but the
    # srcdoc strings it builds are HTML documents whose embedded scripts still need the
    # escape. Any literal close here is an unescaped srcdoc bug.
    assert "</script>" not in art._SHELL_JS, "escape srcdoc script closes as <\\/script>"


# ── render feedback: the code→render→fix loop (#1458) ───────────────────────────


def _client(art):
    from fastapi.testclient import TestClient

    return TestClient(_app(art))


def test_render_status_route_stamps_the_version(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    c = _client(art)
    art.show_artifact.invoke({"kind": "react", "code": "x"})
    aid = art._read_store()["artifacts"][0]["id"]
    r = c.post(
        "/api/plugins/artifact/render-status",
        json={"id": aid, "version": 1, "ok": False, "error": "Icon is not defined"},
    )
    assert r.status_code == 200 and r.json()["recorded"] is True
    rec = art._read_store()["artifacts"][0]["versions"][0]["render"]
    assert rec["ok"] is False and rec["error"] == "Icon is not defined" and rec["ts"] > 0


def test_render_status_unknown_id_or_version_is_a_noop(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    c = _client(art)
    art.show_artifact.invoke({"kind": "html", "code": "x"})
    aid = art._read_store()["artifacts"][0]["id"]
    assert (
        c.post("/api/plugins/artifact/render-status", json={"id": aid, "version": 9, "ok": True}).json()["recorded"]
        is False
    )
    assert (
        c.post("/api/plugins/artifact/render-status", json={"id": "nope", "version": 1, "ok": True}).json()["recorded"]
        is False
    )
    assert "render" not in art._read_store()["artifacts"][0]["versions"][0]


def test_render_error_string_is_capped(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    c = _client(art)
    art.show_artifact.invoke({"kind": "html", "code": "x"})
    aid = art._read_store()["artifacts"][0]["id"]
    c.post("/api/plugins/artifact/render-status", json={"id": aid, "version": 1, "ok": False, "error": "E" * 9000})
    assert len(art._read_store()["artifacts"][0]["versions"][0]["render"]["error"]) == art._RENDER_ERR_MAX


def test_check_artifact_reports_each_render_state(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert "No artifact to check" in art.check_artifact.invoke({})
    art.show_artifact.invoke({"kind": "react", "code": "x"})
    aid = art._read_store()["artifacts"][0]["id"]
    c = _client(art)
    assert "no render result yet" in art.check_artifact.invoke({})
    c.post("/api/plugins/artifact/render-status", json={"id": aid, "version": 1, "ok": True})
    assert "rendered cleanly" in art.check_artifact.invoke({})
    art.update_artifact.invoke({"old_string": "x", "new_string": "y"})  # v2: status resets
    assert "no render result yet" in art.check_artifact.invoke({})
    c.post("/api/plugins/artifact/render-status", json={"id": aid, "version": 2, "ok": False, "error": "boom"})
    out = art.check_artifact.invoke({})
    assert "render FAILED" in out and "boom" in out and "v2" in out


def test_create_reply_surfaces_render_error_when_renderer_live(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "react", "code": "x"})
    aid = art._read_store()["artifacts"][0]["id"]
    # a live renderer + an already-recorded error ⇒ the inline verdict surfaces it
    store = art._read_store()
    store["artifacts"][0]["versions"][0]["render"] = {"ok": False, "error": "Icon is not defined", "ts": art._now()}
    art._write_store(store)
    art._render_status._LAST_POLL_TS = art._now()
    suffix = art._render_suffix(aid, 1)
    assert "FAILED to render" in suffix and "Icon is not defined" in suffix
    # a clean render reads as such
    store = art._read_store()
    store["artifacts"][0]["versions"][0]["render"] = {"ok": True, "error": "", "ts": art._now()}
    art._write_store(store)
    assert "rendered cleanly" in art._render_suffix(aid, 1)


def test_render_wait_is_skipped_when_no_renderer(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._render_status._LAST_POLL_TS = 0  # no panel poll ⇒ headless ⇒ no wait, no inline verdict
    assert art._renderer_live() is False
    out = art.show_artifact.invoke({"kind": "react", "code": "x"})
    assert "FAILED to render" not in out and "rendered cleanly" not in out


def test_history_poll_marks_a_renderer_live(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert art._renderer_live() is False
    _client(art).get("/api/plugins/artifact/history")
    assert art._renderer_live() is True


# ── react auto-mount + proactive verify (forgiving renderer) ────────────────────


def test_react_srcdoc_auto_mounts_app(monkeypatch, tmp_path):
    """The react harness auto-mounts a top-level `App` when the artifact defined it but never
    called render() — the #1 first-try failure. Fires only if #root is still empty (an explicit
    render wins), and routes any throw through __artErr."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    assert 'typeof App!=="undefined"' in html
    assert "React.createElement(App)" in html
    assert "if(r.firstChild)return;" in html  # never double-mounts a self-mounting artifact
    # the no-mount guard now points at the App convention
    assert "name your top-level component `App` (it auto-mounts)" in html


def test_check_artifact_waits_for_a_live_render(monkeypatch, tmp_path):
    """check_artifact returns a stored verdict regardless of live-ness, and (when nothing is
    recorded yet) waits via _await_render only if a renderer is live."""
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "react", "code": "x"})
    aid = art._read_store()["artifacts"][0]["id"]
    # stored verdict is read even when no renderer is live
    store = art._read_store()
    store["artifacts"][0]["versions"][0]["render"] = {"ok": True, "error": "", "ts": art._now()}
    art._write_store(store)
    art._render_status._LAST_POLL_TS = 0
    assert "rendered cleanly" in art.check_artifact.invoke({"artifact_id": aid})


def test_live_theme_repush_is_wired(monkeypatch, tmp_path):
    """App-theme switches re-theme the NESTED artifact frame in place (#1872): base()
    bakes tokens in as literals at render time, so without a push the frame kept the
    stale palette. The shell observes the kit's token rewrite on the root element and
    postMessages fresh tokens; the SHIM applies them without a re-srcdoc (interactive
    artifact state survives)."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    # frame side: the SHIM handles the theme message and updates :root vars in place.
    assert "protoArtifact:theme" in html
    assert "st.setProperty(k,String(m.tokens[k]))" in html
    # shell side: observe the kit re-theme + re-push after a fresh srcdoc load.
    assert "new MutationObserver(pushTheme).observe(document.documentElement" in html
    assert '$frame.addEventListener("load", pushTheme)' in html


# ── file artifacts (ADR 0092 D2): save_file_artifact + sidecar blobs + blob route ─


def test_save_file_artifact_missing_file(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = art.save_file_artifact.invoke({"path": str(tmp_path / "nope.docx")})
    assert "No file at" in out
    assert _arts(art) == []


def test_save_file_artifact_creates_file_kind_with_blob_and_preview(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "notes.txt"
    f.write_text("hello world\nsecond line", encoding="utf-8")
    out = art.save_file_artifact.invoke({"path": str(f), "title": "My Notes"})
    assert "Saved file artifact" in out
    a = _arts(art)[0]
    assert a["kind"] == "file" and a["title"] == "My Notes"
    v = a["versions"][0]
    # text preview lands verbatim in `code` (diffable); file metadata + blob token alongside.
    assert "hello world" in v["code"]
    assert v["file"]["filename"] == "notes.txt" and v["file"]["size"] == len(f.read_bytes())
    assert v["file"]["mime"].startswith("text/")
    assert v["blob"]  # sidecar token
    # the bytes live on disk under blobs/<id>/<token>, NOT inlined in the store
    blob = art._blob_path(a["id"], v["blob"])
    assert blob.exists() and blob.read_bytes() == f.read_bytes()


def test_save_file_artifact_revision_appends_version(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "r.txt"
    f.write_text("v1 body", encoding="utf-8")
    art.save_file_artifact.invoke({"path": str(f)})
    aid = _arts(art)[0]["id"]
    f.write_text("v2 body changed", encoding="utf-8")
    art.save_file_artifact.invoke({"path": str(f), "artifact_id": aid})
    a = _arts(art)[0]
    assert a["id"] == aid and len(a["versions"]) == 2
    assert "v1 body" in a["versions"][0]["code"] and "v2 body changed" in a["versions"][1]["code"]
    # each version keeps its OWN blob (distinct tokens), both on disk
    b1, b2 = a["versions"][0]["blob"], a["versions"][1]["blob"]
    assert b1 != b2
    assert art._blob_path(aid, b1).exists() and art._blob_path(aid, b2).exists()


def test_unknown_revision_target_is_rejected(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("x", encoding="utf-8")
    out = art.save_file_artifact.invoke({"path": str(f), "artifact_id": "a-nope"})
    assert "No artifact" in out and _arts(art) == []


def test_oversized_file_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_BLOB_KB", "1")
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 4096)
    out = art.save_file_artifact.invoke({"path": str(f)})
    assert "too large" in out.lower() and _arts(art) == []


def test_blob_gc_drops_orphaned_and_deleted(monkeypatch, tmp_path):
    """Trimming past max_versions and deleting an artifact both sweep their sidecar blobs."""
    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", "2")
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "g.txt"
    for i in range(3):  # 3 revisions, cap is 2 → the oldest version's blob is orphaned
        f.write_text(f"rev {i}", encoding="utf-8")
        art.save_file_artifact.invoke({"path": str(f), "artifact_id": (_arts(art)[0]["id"] if _arts(art) else "")})
    a = _arts(art)[0]
    assert len(a["versions"]) == 2  # trimmed
    # exactly the 2 surviving version blobs remain on disk
    live = {v["blob"] for v in a["versions"]}
    on_disk = {p.name for p in (art._blob_root() / a["id"]).iterdir()}
    assert on_disk == live
    # deleting the artifact drops its whole blob dir
    art.delete_artifact.invoke({"artifact_id": a["id"]})
    assert not (art._blob_root() / a["id"]).exists()


def test_blob_route_serves_bytes_with_filename(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "report.csv"
    f.write_bytes(b"a,b\n1,2\n")
    art.save_file_artifact.invoke({"path": str(f), "title": "Report"})
    aid = _arts(art)[0]["id"]
    c = TestClient(_app(art))
    r = c.get(f"/api/plugins/artifact/artifact/{aid}/blob")
    assert r.status_code == 200
    assert r.content == b"a,b\n1,2\n"
    assert "report.csv" in r.headers.get("content-disposition", "")
    # unknown artifact / non-file artifact → 404
    assert c.get("/api/plugins/artifact/artifact/nope/blob").status_code == 404
    art.show_artifact.invoke({"kind": "html", "code": "<p>x</p>"})
    html_id = _arts(art)[0]["id"]
    assert c.get(f"/api/plugins/artifact/artifact/{html_id}/blob").status_code == 404


def test_thumbnail_only_for_images(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    # a text file never gets a thumbnail
    f = tmp_path / "t.txt"
    f.write_text("plain", encoding="utf-8")
    art.save_file_artifact.invoke({"path": str(f)})
    assert _arts(art)[0]["versions"][0]["file"]["thumb"] == ""
    # an image gets a base64 PNG data-URI thumbnail (skip if Pillow isn't installed)
    Image = pytest.importorskip("PIL.Image")
    img = tmp_path / "pic.png"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(str(img))
    art.save_file_artifact.invoke({"path": str(img)})
    thumb = _arts(art)[0]["versions"][0]["file"]["thumb"]
    assert thumb.startswith("data:image/png;base64,")


def test_save_file_artifact_registered_and_shell_has_filecard(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    names = []

    class _Reg:
        def register_tool(self, t):
            names.append(t.name)

        def register_skill_dir(self, *a, **k):
            pass

        def register_router(self, *a, **k):
            pass

        def emit(self, *a, **k):
            pass

    art.register(_Reg())
    assert "save_file_artifact" in names
    # the shell renders file kinds as a download card, hides Edit, and downloads via the blob route
    html = art._SHELL_HTML + art._SHELL_JS
    assert "function fileCard(v)" in html
    assert 'a.kind==="file" ? fileCard(v) : srcdoc(a.kind, v.code)' in html
    assert "/blob?version=" in html


def test_docx_extraction_reads_paragraphs(monkeypatch, tmp_path):
    docx = pytest.importorskip("docx")  # python-docx; present on the desktop stack (ADR 0092 D1)
    art = _load(monkeypatch, tmp_path)
    d = docx.Document()
    d.add_paragraph("First para of the report.")
    d.add_paragraph("Second para with detail.")
    p = tmp_path / "r.docx"
    d.save(str(p))
    art.save_file_artifact.invoke({"path": str(p)})
    v = _arts(art)[0]["versions"][0]
    assert "First para of the report." in v["code"] and "Second para" in v["code"]
    assert _arts(art)[0]["kind"] == "file"


def test_xlsx_extraction_reads_sheet_cells(monkeypatch, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    art = _load(monkeypatch, tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Region", "Total"])
    ws.append(["West", 1200])
    p = tmp_path / "s.xlsx"
    wb.save(str(p))
    art.save_file_artifact.invoke({"path": str(p)})
    code = _arts(art)[0]["versions"][0]["code"]
    assert "# Sales" in code and "Region" in code and "1200" in code


def test_unparseable_office_file_degrades_not_crashes(monkeypatch, tmp_path):
    """A .docx the extractor can't parse (corrupt, or python-docx absent in a lean env) still
    saves — the preview is a readable degrade note, the bytes are stored, no crash."""
    art = _load(monkeypatch, tmp_path)
    p = tmp_path / "broken.docx"
    p.write_bytes(b"not really a docx")
    out = art.save_file_artifact.invoke({"path": str(p)})
    assert "Saved file artifact" in out
    v = _arts(art)[0]["versions"][0]
    assert "no text preview" in v["code"].lower()
    assert art._blob_path(_arts(art)[0]["id"], v["blob"]).read_bytes() == b"not really a docx"


def test_pptx_extraction_reads_slide_text(monkeypatch, tmp_path):
    pptx = pytest.importorskip("pptx")  # python-pptx; present on the desktop stack (ADR 0092 D1)
    art = _load(monkeypatch, tmp_path)
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title-only layout
    slide.shapes.title.text = "Quarterly Roadmap"
    prs.save(str(tmp_path / "deck.pptx"))
    art.save_file_artifact.invoke({"path": str(tmp_path / "deck.pptx")})
    code = _arts(art)[0]["versions"][0]["code"]
    assert "Slide 1" in code and "Quarterly Roadmap" in code


def test_pdf_extraction_reads_text(monkeypatch, tmp_path):
    pytest.importorskip("pypdf")
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")  # generate a real PDF to read back
    art = _load(monkeypatch, tmp_path)
    p = tmp_path / "doc.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 720, "Invoice total due")
    c.save()
    art.save_file_artifact.invoke({"path": str(p)})
    assert "Invoice total due" in _arts(art)[0]["versions"][0]["code"]


# ── kind-confusion guards (protoreview #2126: both _commit_version directions) ──


def test_save_file_artifact_refuses_to_revise_a_non_file_artifact(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>hi</p>"})
    html_id = _arts(art)[0]["id"]
    f = tmp_path / "x.txt"
    f.write_text("body", encoding="utf-8")
    out = art.save_file_artifact.invoke({"path": str(f), "artifact_id": html_id})
    assert "not a file" in out
    a = art._find(art._read_store(), html_id)
    assert a["kind"] == "html" and len(a["versions"]) == 1  # untouched, not corrupted


def test_text_edits_refuse_a_file_artifact(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "r.txt"
    f.write_text("original", encoding="utf-8")
    art.save_file_artifact.invoke({"path": str(f)})
    fid = _arts(art)[0]["id"]
    assert "file artifact" in art.update_artifact.invoke(
        {"old_string": "original", "new_string": "x", "artifact_id": fid}
    )
    assert "file artifact" in art.rewrite_artifact.invoke({"code": "x", "artifact_id": fid})
    # the panel PUT route is guarded too (the hidden Edit button is only a client mask)
    c = TestClient(_app(art))
    r = c.put(f"/api/plugins/artifact/artifact/{fid}", json={"code": "x"})
    assert r.status_code == 409
    # still a single, intact file version with its blob
    a = _arts(art)[0]
    assert a["kind"] == "file" and len(a["versions"]) == 1 and a["versions"][0]["blob"]


def test_clip_truncates_on_a_codepoint_boundary(monkeypatch, tmp_path):
    """_clip must not split a multi-byte char at the byte cut (protoreview #2126): a
    4-byte emoji straddling the budget is excluded cleanly, not decoded to a broken char."""
    art = _load(monkeypatch, tmp_path)
    note_len = len(art._PREVIEW_TRUNC.encode())
    monkeypatch.setattr(art._config, "_max_preview_bytes", lambda: note_len + 5)  # 5-byte body budget
    out = art._clip("😀" * 30)  # 4-byte codepoints, well over budget — byte-5 cut splits the 2nd
    assert out.endswith(art._PREVIEW_TRUNC)
    body = out[: -len(art._PREVIEW_TRUNC)]
    assert body == "😀"  # exactly one whole emoji fits; the straddling one is dropped, not mangled
    body.encode("utf-8")  # valid utf-8 round-trips (no partial sequence)


def test_blob_gc_isolates_a_failing_dir(monkeypatch, tmp_path):
    """A failure sweeping one blob dir must not abort GC of the others (protoreview #2126):
    per-directory error isolation, so a stray un-drainable dir can't strand every orphan."""
    art = _load(monkeypatch, tmp_path)
    root = art._blob_root()
    bad = root / "a-bad"
    bad.mkdir(parents=True)
    (bad / "sub").mkdir()  # a subdir → f.unlink() raises OSError while draining this orphan
    good = root / "a-good"
    good.mkdir()
    (good / "v.bin").write_bytes(b"x")
    art._gc_blobs({"artifacts": []})  # both are orphans (empty store)
    assert not good.exists()  # the good orphan was still swept despite bad failing (any order)
    assert bad.exists()  # bad couldn't be removed, but didn't abort the sweep


def test_blob_route_version_bounds(monkeypatch, tmp_path):
    """An explicit out-of-range version 404s per the route's docstring, not silently the
    latest (protoreview #2126); absent/0 → latest, in-range → that version."""
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "d.txt"
    f.write_text("v1", encoding="utf-8")
    art.save_file_artifact.invoke({"path": str(f)})
    aid = _arts(art)[0]["id"]
    f.write_text("v2", encoding="utf-8")
    art.save_file_artifact.invoke({"path": str(f), "artifact_id": aid})
    c = TestClient(_app(art))
    base = f"/api/plugins/artifact/artifact/{aid}/blob"
    assert c.get(base).content == b"v2"  # absent → latest
    assert c.get(base + "?version=0").content == b"v2"  # 0 → latest
    assert c.get(base + "?version=1").content == b"v1"  # explicit in-range
    assert c.get(base + "?version=99").status_code == 404  # out of range → 404 (not latest)


# ── resolve_for_bundle (#2681 — the chat-bundle consumption seam) ───────────────


def test_resolve_for_bundle_unknown_id(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = art.resolve_for_bundle("a-nope-000000", None)
    assert out == {"id": "a-nope-000000", "available": False, "reason": "artifact no longer exists"}


def test_resolve_for_bundle_fresh_creation(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<h1>Hi</h1>", "title": "T"})
    aid = _arts(art)[0]["id"]
    out = art.resolve_for_bundle(aid, None)
    assert out == {
        "id": aid,
        "kind": "html",
        "title": "T",
        "version": 1,
        "available": True,
        "code": "<h1>Hi</h1>",
        "by": "agent",
    }


def test_resolve_for_bundle_exact_version_after_update(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<h1>Hello</h1>"})
    aid = _arts(art)[0]["id"]
    art.update_artifact.invoke({"old_string": "Hello", "new_string": "World"})
    v1 = art.resolve_for_bundle(aid, 1)
    v2 = art.resolve_for_bundle(aid, 2)
    assert v1["available"] and v1["code"] == "<h1>Hello</h1>"
    assert v2["available"] and v2["code"] == "<h1>World</h1>"


def test_resolve_for_bundle_out_of_range_version(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<x/>"})
    aid = _arts(art)[0]["id"]
    out = art.resolve_for_bundle(aid, 99)
    assert out["available"] is False and "no longer available" in out["reason"]


def test_resolve_for_bundle_file_kind_is_placeholder_only(monkeypatch, tmp_path):
    """Josh's P2 scoping call: binary attachments are a note (kind/size/filename), never
    the bytes, in this slice."""
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake pdf bytes")
    art.save_file_artifact.invoke({"path": str(f), "title": "Report"})
    aid = _arts(art)[0]["id"]
    out = art.resolve_for_bundle(aid, 1)
    assert out["available"] is False
    assert out["kind"] == "file"
    assert "not included" in out["reason"]
    assert "code" not in out and "content" not in out  # no bytes, no preview text
    assert out["file_meta"]["filename"] == "report.pdf"


def test_resolve_for_bundle_detects_trim_and_reports_unavailable(monkeypatch, tmp_path):
    """Once an artifact's version count has ever exceeded its retention cap, version
    NUMBERS become ambiguous, not just the evicted one — two different commits can report
    the same post-trim length. Must degrade to 'unavailable' for the WHOLE artifact's
    numbered lookups from then on, never risk matching a reference to the wrong revision."""
    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", "2")
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "v1"})
    aid = _arts(art)[0]["id"]
    art.update_artifact.invoke({"old_string": "v1", "new_string": "v2"})
    art.update_artifact.invoke({"old_string": "v2", "new_string": "v3"})  # trims v1 away
    assert len(_arts(art)[0]["versions"]) == 2  # cap held
    out = art.resolve_for_bundle(aid, 1)  # the original "version 1" no longer exists
    assert out["available"] is False and "trimmed" in out["reason"]
    # "version 2" is now AMBIGUOUS (both the evicted v2 and the surviving v3 reported that
    # same post-trim length) — also unavailable, not a lucky guess at the wrong content.
    ambiguous = art.resolve_for_bundle(aid, 2)
    assert ambiguous["available"] is False and "trimmed" in ambiguous["reason"]


# ── typed file previews (v0.17.0): csv/tsv table, md prose, json pretty ────────


def test_text_ext_covers_data_and_code_files(monkeypatch, tmp_path):
    """.tsv and .py previews decode verbatim — not the '(binary file …)' note —
    regardless of what the platform's mimetypes table says about the extension."""
    art = _load(monkeypatch, tmp_path)
    for name, body in (("t.tsv", "a\tb\n1\t2"), ("s.py", "print('hi')")):
        f = tmp_path / name
        # newline="\n" pins LF on disk — Windows' default translation writes CRLF and
        # the verbatim-preview equality below would fail there (the CRLF trap, cf. #2814).
        f.write_text(body, encoding="utf-8", newline="\n")
        art.save_file_artifact.invoke({"path": str(f)})
        assert _arts(art)[0]["versions"][0]["code"] == body


def test_shell_types_file_previews(monkeypatch, tmp_path):
    """fileCard dispatches by extension: DSV parser + row-capped table for csv/tsv,
    mdDoc reuse for .md, JSON pretty-print, text fallback."""
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML + art._SHELL_JS
    assert "function parseDsv(" in html
    assert "function previewKind(" in html
    assert "TABLE_MAX_ROWS" in html
    assert "return mdDoc(" in html  # .md files reuse the markdown kind's renderer
    assert "JSON.stringify(JSON.parse(" in html


def test_shell_trunc_marker_mirrors_python(monkeypatch, tmp_path):
    """The shell strips the preview-truncation note before parsing a table/json —
    its marker string must stay a literal mirror of _PREVIEW_TRUNC or clipped
    previews would feed the note into the parsers."""
    art = _load(monkeypatch, tmp_path)
    mark = "(preview truncated — download the file for the full content)"
    assert art._PREVIEW_TRUNC.endswith(mark)
    assert '"' + mark + '"' in art._SHELL_JS


def test_every_view_subresource_is_auth_exempt(monkeypatch, tmp_path):
    """The view PAGE is public, but the requests it spawns — <script src>, ES-module
    imports — carry no Authorization header, so any gated subresource 401s and the
    panel boots dead (empty picker, no artifacts). vendor/ learned this first; the
    #2822 shell.js split re-hit it. Guard: every src the shell page references must
    resolve under a manifest public_paths entry."""
    import re

    import yaml

    art = _load(monkeypatch, tmp_path)
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    public = manifest["public_paths"]
    for src in re.findall(r'src="([^"]+)"', art._SHELL_HTML):
        url = src if src.startswith("/") else f"/plugins/artifact/{src}"
        assert any(url.startswith(p) for p in public), f"{url} is auth-gated but fetched tokenless"
    assert "/plugins/artifact/shell.js" in public  # the concrete regression


# ── #3401: parallel store mutations must not clobber each other ──────────────────────
def _same_snapshot_gate(monkeypatch, module, attr):
    """Force two callers to read the SAME snapshot before either writes.

    A plain start-barrier only releases both threads before the call — it does not stop
    one from finishing entirely before the other reads, so an unlocked implementation
    could still pass by luck. Gating INSIDE the read removes that luck: with no lock both
    readers meet at the barrier holding identical text, so the second write must clobber
    the first. With the lock, the second reader can't arrive (the first still holds it),
    the barrier times out, and the edit proceeds correctly — which is why the timeout is
    short and a broken barrier is not an error here.
    """
    import threading

    real = getattr(module, attr)
    barrier = threading.Barrier(2)

    def gated(*args, **kwargs):
        out = real(*args, **kwargs)
        try:
            barrier.wait(timeout=0.3)
        except threading.BrokenBarrierError:
            pass  # serialised: the other caller can't be here, which is the point
        return out

    monkeypatch.setattr(module, attr, gated)



def test_parallel_update_artifact_calls_both_land(monkeypatch, tmp_path):
    """Two updates to ONE artifact from different threads must both survive.

    Every mutating path is read-whole-store → change → write-whole-store, and the harness
    runs independent tool calls in PARALLEL. Unserialised, both read the same snapshot,
    each appends version N+1 to its own copy, and the second whole-store write overwrites
    the first — losing an edit AND its version while both report success. Both reporting
    the SAME new version is the tell: observed live, two updates each said "version 2".
    """
    import threading

    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<h1>Title</h1>\n<p>Body</p>"})
    _same_snapshot_gate(monkeypatch, art._store, "_read_store")

    start = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def update(old: str, new: str):
        start.wait(timeout=5)
        out = art.update_artifact.invoke({"old_string": old, "new_string": new})
        with lock:
            results.append(out)

    threads = [
        threading.Thread(target=update, args=("<h1>Title</h1>", "<h1>Title</h1>\n<nav>links</nav>")),
        threading.Thread(target=update, args=("<p>Body</p>", "<p>Body text</p>")),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert all("Updated artifact" in r for r in results), results
    code = _arts(art)[0]["versions"][-1]["code"]
    assert "<nav>links</nav>" in code
    assert "<p>Body text</p>" in code
    # Two sequential commits, so two new versions — never the same number twice.
    assert sorted(r.split("version ")[1].rstrip(".") for r in results) == ["2", "3"]


# ── pinning: exempt a long-lived artifact from history eviction ─────────────────────
# Field case (careercoach resume skill): a master resume kept as an HTML artifact, its id
# recorded elsewhere, was silently evicted after ~20 unrelated artifacts — history counts
# EVERY artifact on the instance — and the recorded id then pointed at nothing.


def _show(art, code, title=""):
    art.show_artifact.invoke({"kind": "html", "code": code, "title": title})
    return art._read_store()["current"]  # not artifacts[0]: pinned artifacts are stored first


def _ids(art):
    return [a["id"] for a in _arts(art)]


def test_pinned_artifact_survives_30_unrelated_artifacts(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)  # default history = 20
    resume = _show(art, "<h1>Resume</h1>", "Master resume")
    assert "Pinned artifact" in art.pin_artifact.invoke({"artifact_id": resume})
    unrelated = [_show(art, f"<p>{i}</p>") for i in range(30)]
    ids = _ids(art)
    assert resume in ids
    # The pin doesn't count toward the 20: the 20 newest unpinned are all kept beside it.
    assert set(ids) == {resume, *unrelated[-20:]} and len(ids) == 21
    # The recorded id still reads and still takes edits.
    assert "<h1>Resume</h1>" in art.get_artifact.invoke({"artifact_id": resume})
    out = art.update_artifact.invoke({"old_string": "Resume", "new_string": "Resume v2", "artifact_id": resume})
    assert "version 2" in out


@pytest.mark.parametrize("history", [3, 20])
def test_unpinned_eviction_point_is_unchanged(monkeypatch, tmp_path, history):
    """No-regression: with nothing pinned, eviction is exactly the old ``artifacts[:history]``
    slice over most-recently-touched order — same survivors, same order, after every write."""
    monkeypatch.setenv("ARTIFACT_HISTORY", str(history))
    art = _load(monkeypatch, tmp_path)
    order: list[str] = []
    for i in range(history + 10):
        aid = _show(art, f"<p>{i}</p>")
        order = ([aid] + order)[:history]
        assert _ids(art) == order
    # A touch reorders recency; eviction still follows it exactly as before.
    target = order[-1]
    art.update_artifact.invoke({"old_string": "<p>", "new_string": "<p class=t>", "artifact_id": target})
    order = [target] + [x for x in order if x != target]
    assert _ids(art) == order
    aid = _show(art, "<p>last</p>")
    order = ([aid] + order)[:history]
    assert _ids(art) == order


def test_pin_cap_refuses_and_names_the_held_pins(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_PINNED", "2")
    art = _load(monkeypatch, tmp_path)
    a, b, c = [_show(art, f"<p>{i}</p>", f"T{i}") for i in range(3)]
    art.pin_artifact.invoke({"artifact_id": a})
    art.pin_artifact.invoke({"artifact_id": b})
    out = art.pin_artifact.invoke({"artifact_id": c})
    assert "Can't pin" in out and "2/2" in out and a in out and b in out and "pinned=False" in out
    assert {x["id"] for x in art._store._pinned(art._read_store())} == {a, b}  # refused, not persisted
    assert "already pinned" in art.pin_artifact.invoke({"artifact_id": a})  # idempotent, not a refusal
    # Unpinning frees a slot.
    assert "Unpinned" in art.pin_artifact.invoke({"artifact_id": a, "pinned": False})
    assert "Pinned artifact" in art.pin_artifact.invoke({"artifact_id": c})
    # Lowering the cap below what's held unpins NOTHING (that'd be the same silent loss) —
    # it only refuses new pins.
    monkeypatch.setenv("ARTIFACT_MAX_PINNED", "1")
    assert "Can't pin" in art.pin_artifact.invoke({"artifact_id": a})
    _show(art, "<p>another write</p>")
    assert {x["id"] for x in art._store._pinned(art._read_store())} == {b, c}
    # 0 refuses every NEW pin, but existing pins stay protected; an unknown id is a clean miss.
    monkeypatch.setenv("ARTIFACT_MAX_PINNED", "0")
    out = art.pin_artifact.invoke({"artifact_id": a})
    assert "Can't pin" in out and "max_pinned setting is 0" in out and "stay protected" in out
    _show(art, "<p>a write under cap 0</p>")
    assert {x["id"] for x in art._store._pinned(art._read_store())} == {b, c}
    assert "No artifact" in art.pin_artifact.invoke({"artifact_id": "nope"})


def test_pin_refusal_names_at_most_five_pins(monkeypatch, tmp_path):
    """At a large pin count the refusal must not list every pin (50 pins was ~740 chars)."""
    monkeypatch.setenv("ARTIFACT_MAX_PINNED", "8")
    art = _load(monkeypatch, tmp_path)
    ids = [_show(art, f"<p>{i}</p>", f"T{i}") for i in range(9)]
    for aid in ids[:8]:
        art.pin_artifact.invoke({"artifact_id": aid})
    out = art.pin_artifact.invoke({"artifact_id": ids[8]})
    assert "Can't pin" in out and "8/8" in out and "and 3 more (see list_artifacts)" in out
    assert sum(aid in out for aid in ids[:8]) == 5


def test_pinned_artifacts_are_stored_first_so_a_downgrade_keeps_them(monkeypatch, tmp_path):
    """Downgrade guard. A pre-0.18 plugin knows nothing of pins: every write it makes keeps just
    ``artifacts[:history]``. A long-lived pinned artifact is usually the OLDEST touched, so in
    recency order that first old write would evict it (and GC a file artifact's blobs). Stored
    pins-first, the old slice keeps them until `history` newer artifacts push them out."""
    art = _load(monkeypatch, tmp_path)  # default history = 20
    pins = [_show(art, f"<p>pin {i}</p>", f"P{i}") for i in range(3)]
    for p in pins:
        art.pin_artifact.invoke({"artifact_id": p})
    for i in range(25):
        _show(art, f"<p>{i}</p>")
    arts = _arts(art)
    assert {a["id"] for a in arts[:3]} == set(pins) and all(a.get("pinned") is True for a in arts[:3])
    assert [a["id"] for a in arts[:3]] == list(reversed(pins))  # the pinned group stays newest-first

    # Simulate the OLD plugin's writes: its show_artifact inserts at the front, then [:history].
    old = arts
    for k in range(17):  # 17 new artifacts + 3 pins = exactly the 20 it keeps
        old = ([{"id": f"old-{k}", "versions": [{"code": "x"}]}] + old)[:20]
        assert set(pins) <= {a["id"] for a in old}, f"a pin was evicted by old write #{k + 1}"


def test_max_pinned_config_default_zero_and_bad_values(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert art._max_pinned() == 10
    monkeypatch.setenv("ARTIFACT_MAX_PINNED", "0")
    assert art._max_pinned() == 0  # 0 is legal here (disables), unlike the other caps
    monkeypatch.setenv("ARTIFACT_MAX_PINNED", "lots")
    assert art._max_pinned() == 10  # bad value → default, never crashes


def test_unpin_takes_the_most_recent_slot_instead_of_evicting(monkeypatch, tmp_path):
    """A long-pinned artifact sits behind every newer one. Unpinned in place, the very write
    that unpins it would evict it — so unpinning gives it the most-recent slot (a full
    history window) without stealing the panel's current focus."""
    monkeypatch.setenv("ARTIFACT_HISTORY", "3")
    art = _load(monkeypatch, tmp_path)
    keep = _show(art, "<p>keep</p>")
    art.pin_artifact.invoke({"artifact_id": keep})
    for i in range(5):
        _show(art, f"<p>{i}</p>")
    current = art._read_store()["current"]
    art.pin_artifact.invoke({"artifact_id": keep, "pinned": False})
    store = art._read_store()
    assert store["artifacts"][0]["id"] == keep and "pinned" not in store["artifacts"][0]
    assert store["current"] == current
    assert len(store["artifacts"]) == 3
    for i in range(3):  # …and from here it evicts like any other artifact
        _show(art, f"<p>after {i}</p>")
    assert keep not in _ids(art)


def test_unpin_survives_a_store_left_by_an_older_plugin(monkeypatch, tmp_path):
    """This code keeps pins first, but a store last written by a pre-0.18 plugin (a downgrade,
    then an upgrade) isn't: the old plugin inserted its new artifacts AHEAD of the pin. Unpinning
    in place there would evict the artifact on that very write — it must take the front slot."""
    import json

    monkeypatch.setenv("ARTIFACT_HISTORY", "3")
    art = _load(monkeypatch, tmp_path)
    keep = _show(art, "<p>keep</p>")
    art.pin_artifact.invoke({"artifact_id": keep})
    for i in range(3):
        _show(art, f"<p>{i}</p>")
    raw = json.loads(art._store_path().read_text(encoding="utf-8"))
    pin = next(a for a in raw["artifacts"] if a["id"] == keep)
    raw["artifacts"] = [a for a in raw["artifacts"] if a["id"] != keep] + [pin]  # the old plugin's order
    art._store_path().write_text(json.dumps(raw), encoding="utf-8")
    art.pin_artifact.invoke({"artifact_id": keep, "pinned": False})
    assert _ids(art)[0] == keep and len(_ids(art)) == 3


def test_pin_does_not_exempt_versions_from_max_versions(monkeypatch, tmp_path):
    """Deliberate: a pin keeps the ARTIFACT, not every edit — versions still trim to
    max_versions, so a daily-edited pinned doc can't grow history.json without bound."""
    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", "3")
    art = _load(monkeypatch, tmp_path)
    aid = _show(art, "v0")
    art.pin_artifact.invoke({"artifact_id": aid})
    for i in range(1, 6):
        art.rewrite_artifact.invoke({"code": f"v{i}", "artifact_id": aid})
    a = art._find(art._read_store(), aid)
    assert a["pinned"] is True
    assert [v["code"] for v in a["versions"]] == ["v3", "v4", "v5"]


def test_list_get_and_history_report_pinned_state(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    plain = _show(art, "<p>plain</p>", "Plain")
    resume = _show(art, "<p>resume</p>", "Resume")
    art.pin_artifact.invoke({"artifact_id": resume})
    listing = art.list_artifacts.invoke({})
    rows = {ln.split()[0]: ln for ln in listing.splitlines() if ln.startswith("a-")}
    assert "· pinned" in rows[resume] and "· pinned" not in rows[plain]
    assert "1/10 pins used" in listing and "20 most recently touched unpinned" in listing
    assert "· pinned" in art.get_artifact.invoke({"artifact_id": resume}).splitlines()[0]
    assert "pinned" not in art.get_artifact.invoke({"artifact_id": plain}).splitlines()[0]
    # The panel's /history payload (the raw store) carries it as well.
    by_id = {a["id"]: a for a in _client(art).get("/api/plugins/artifact/history").json()["artifacts"]}
    assert by_id[resume]["pinned"] is True and "pinned" not in by_id[plain]


def test_pre_pin_store_loads_and_evicts_as_before(monkeypatch, tmp_path):
    """Backward compat: a store written before pinning existed (no ``pinned`` key anywhere)
    loads unchanged — nothing pinned — and the next write evicts at the same point as ever."""
    import json

    monkeypatch.setenv("ARTIFACT_HISTORY", "3")
    art = _load(monkeypatch, tmp_path)
    old = {
        "artifacts": [
            {
                "id": f"old{i}",
                "title": f"O{i}",
                "kind": "html",
                "versions": [{"code": f"<p>{i}</p>", "ts": i, "by": "agent"}],
                "version_count": 1,
                "created": i,
                "updated": i,
            }
            for i in range(5)
        ],
        "current": "old0",
    }
    art._store_path().write_text(json.dumps(old), encoding="utf-8")
    store = art._read_store()
    assert [a["id"] for a in store["artifacts"]] == [f"old{i}" for i in range(5)]  # a read never trims
    assert art._store._pinned(store) == []
    assert "0/10 pins used" in art.list_artifacts.invoke({})
    new = _show(art, "<p>new</p>")
    assert _ids(art) == [new, "old0", "old1"]


def test_pin_persists_as_one_additive_key(monkeypatch, tmp_path):
    """Downgrade safety: pinning changes the persisted shape by exactly ONE key on the pinned
    artifact (top level stays {artifacts, current}), and unpinning removes the key rather than
    writing false — so a pre-pin plugin reading this store sees only a key it ignores."""
    import json

    art = _load(monkeypatch, tmp_path)
    aid = _show(art, "<p>x</p>")
    before = json.loads(art._store_path().read_text(encoding="utf-8"))
    art.pin_artifact.invoke({"artifact_id": aid})
    after = json.loads(art._store_path().read_text(encoding="utf-8"))
    assert set(after) == {"artifacts", "current"}
    assert set(after["artifacts"][0]) == set(before["artifacts"][0]) | {"pinned"}
    assert after["artifacts"][0]["pinned"] is True
    art.pin_artifact.invoke({"artifact_id": aid, "pinned": False})
    assert json.loads(art._store_path().read_text(encoding="utf-8")) == before


def test_only_a_literal_true_pins(monkeypatch, tmp_path):
    """A hand-edited truthy value must not pin an artifact past the max_pinned cap."""
    import json

    monkeypatch.setenv("ARTIFACT_HISTORY", "1")
    art = _load(monkeypatch, tmp_path)
    aid = _show(art, "<p>x</p>")
    raw = json.loads(art._store_path().read_text(encoding="utf-8"))
    raw["artifacts"][0]["pinned"] = "yes"
    art._store_path().write_text(json.dumps(raw), encoding="utf-8")
    _show(art, "<p>y</p>")
    assert aid not in _ids(art)


def test_pinned_file_artifact_keeps_its_blob(monkeypatch, tmp_path):
    """Blob GC follows the SURVIVING artifacts, so a pinned file artifact (a .docx resume)
    keeps its bytes while unpinned neighbours are evicted and swept."""
    monkeypatch.setenv("ARTIFACT_HISTORY", "2")
    art = _load(monkeypatch, tmp_path)
    aid = _saved_id(art.save_file_artifact.invoke({"path": _tmp_file(tmp_path, "resume.txt", b"resume bytes")}))
    art.pin_artifact.invoke({"artifact_id": aid})
    for i in range(4):
        _show(art, f"<p>{i}</p>")
    a = art._find(art._read_store(), aid)
    assert a is not None
    assert art._blob_path(aid, a["versions"][-1]["blob"]).read_bytes() == b"resume bytes"


def test_parallel_pin_and_update_both_land(monkeypatch, tmp_path):
    """pin_artifact is a read-modify-write like every other mutation (#3401): a pin racing an
    edit to the same artifact must not lose either the pin or the new version."""
    import threading

    art = _load(monkeypatch, tmp_path)
    aid = _show(art, "<h1>Resume</h1>")
    _same_snapshot_gate(monkeypatch, art._store, "_read_store")
    start = threading.Barrier(2)
    out: dict[str, str] = {}

    def pin():
        start.wait(timeout=5)
        out["pin"] = art.pin_artifact.invoke({"artifact_id": aid})

    def edit():
        start.wait(timeout=5)
        out["edit"] = art.update_artifact.invoke(
            {"old_string": "Resume", "new_string": "Resume v2", "artifact_id": aid}
        )

    threads = [threading.Thread(target=pin), threading.Thread(target=edit)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)
    a = art._find(art._read_store(), aid)
    assert a.get("pinned") is True, out
    assert a["versions"][-1]["code"] == "<h1>Resume v2</h1>", out


def test_parallel_pins_cannot_overshoot_the_cap(monkeypatch, tmp_path):
    """The cap check and the pin write are one serialised step: two pins racing for the last
    slot → exactly one succeeds and the other is REFUSED — never two 'Pinned' replies of
    which the store kept only one."""
    import threading

    monkeypatch.setenv("ARTIFACT_MAX_PINNED", "1")
    art = _load(monkeypatch, tmp_path)
    a, b = _show(art, "<p>a</p>"), _show(art, "<p>b</p>")
    _same_snapshot_gate(monkeypatch, art._store, "_read_store")
    start = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def pin(aid):
        start.wait(timeout=5)
        r = art.pin_artifact.invoke({"artifact_id": aid})
        with lock:
            results.append(r)

    threads = [threading.Thread(target=pin, args=(x,)) for x in (a, b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)
    assert sorted("Pinned artifact" in r for r in results) == [False, True], results
    assert len(art._store._pinned(art._read_store())) == 1
