"""Tests for the artifact plugin — the tool, the history store, the route split,
and the plugin-view contract (the regression guard for the /api-vs-/plugins mount
bug). Run with: pytest (needs fastapi + langchain_core, the host's deps).

Artifact is bundled into core under ``plugins/artifact/`` (protoAgent #1443), so
ROOT anchors there off the repo root rather than the test's parent dir."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "plugins" / "artifact"


def _load(monkeypatch, tmp_path):
    """Fresh module bound to a temp ARTIFACT_DIR so history is isolated per test."""
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    spec = importlib.util.spec_from_file_location(
        "artifact_under_test", ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    # No real browser in tests → never block a tool on the async render verdict (#1458). The
    # render-feedback tests drive the store directly; _await_render still returns an already-
    # recorded result on its first (pre-sleep) check.
    mod._RENDER_WAIT_MS = 0
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
    assert "No artifact" in art.update_artifact.invoke(
        {"old_string": "a", "new_string": "b"}
    )


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
    art.update_artifact.invoke(
        {"old_string": "first", "new_string": "FIRST", "artifact_id": first_id}
    )
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
    art.update_artifact.invoke(
        {"old_string": "2", "new_string": "9", "artifact_id": _arts(art)[0]["id"]}
    )
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
    assert (
        len(versions) == 3 and versions[-1]["code"] == "v4"
    )  # oldest trimmed, newest kept


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
    monkeypatch.setattr(art, "_plugin_cfg", lambda: {"ask_enabled": True, "history": 7})
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
    assert (
        cur["code"] == "<y/>" and cur["version"] == 2
    )  # latest version of the focused artifact
    hist = c.get("/api/plugins/artifact/history").json()
    assert len(hist["artifacts"]) == 1 and len(hist["artifacts"][0]["versions"]) == 2
    assert hist["current"] == hist["artifacts"][0]["id"]


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
    r = c.put(
        f"/api/plugins/artifact/artifact/{aid}", json={"code": "<p>v2 by user</p>"}
    )
    assert r.status_code == 200 and r.json()["version"] == 2
    a = art._read_store()["artifacts"][0]
    assert a["versions"][-1] == {
        **a["versions"][-1],
        "code": "<p>v2 by user</p>",
        "by": "user",
    }
    assert a["versions"][0]["code"] == "<p>v1</p>"  # agent's v1 preserved (no clobber)
    # unknown id → 404; oversize → 413.
    assert (
        c.put("/api/plugins/artifact/artifact/nope", json={"code": "x"}).status_code
        == 404
    )
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
    assert (
        c2.post(
            "/api/plugins/artifact/ask", json={"prompt": "way too long"}
        ).status_code
        == 413
    )


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
    html = art._SHELL_HTML
    # rule 4 — the same-origin DS kit, base-prefixed by hand (loads before the kit).
    assert "/_ds/plugin-kit.css" in html
    assert "/_ds/plugin-kit.js" in html
    assert 'location.pathname.split("/plugins/")[0]' in html
    # ESM — dynamic import, never a classic <script src> (protoContent#224).
    assert 'import(window.__base + "/_ds/plugin-kit.js")' in html
    assert 'type="module"' in html
    # rules 2+3 — gated data via the kit's slug-aware authed fetch.
    assert 'apiFetch("/api/plugins/artifact/history")' in html
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
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
    # the editor overlays the stage, so editing never needs to tear the frame down.
    assert "#editor{position:absolute;inset:0" in html
    # exitEdit must not force a re-render of the (un-changed) frame on exit — the
    # `…display="none"; lastRendered=""; render()` signature that caused the black panel.
    assert 'lastRendered=""; render()' not in html


def test_ask_bridge_is_wired(monkeypatch, tmp_path):
    """The window.protoArtifact.ask shim is injected into artifacts and the shell
    relays it to the gated /ask endpoint (the agent-callback bridge)."""
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
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
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
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
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
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
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
    assert 'type="importmap"' in html
    assert (
        'data-type="module"' in html
    )  # babel compiles to a module → top-level import ok
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
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
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
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
    assert 'import { marked } from "marked"' in html
    assert "marked.mjs" in html and 'id="md"' in html
    assert "atob(" in html and "btoa(" in html  # base64 round-trip of the source
    assert "language-mermaid" in html  # ```mermaid fences upgrade to live diagrams


def test_ds_kit_injected_into_artifacts(monkeypatch, tmp_path):
    """html/react/markdown artifacts link the same-origin DS plugin-kit stylesheet so the
    `.pl-*` classes + `--pl-*` tokens work inside the sandbox and match the console theme."""
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
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
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
    assert html.count("</script>") == 2, (
        "exactly the slug-base + main module <script> closes; an extra literal </script> "
        "(comment/string) would close the module early — escape it as <\\/script>"
    )


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
    assert c.post("/api/plugins/artifact/render-status", json={"id": aid, "version": 9, "ok": True}).json()["recorded"] is False
    assert c.post("/api/plugins/artifact/render-status", json={"id": "nope", "version": 1, "ok": True}).json()["recorded"] is False
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
    art._LAST_POLL_TS = art._now()
    suffix = art._render_suffix(aid, 1)
    assert "FAILED to render" in suffix and "Icon is not defined" in suffix
    # a clean render reads as such
    store = art._read_store()
    store["artifacts"][0]["versions"][0]["render"] = {"ok": True, "error": "", "ts": art._now()}
    art._write_store(store)
    assert "rendered cleanly" in art._render_suffix(aid, 1)


def test_render_wait_is_skipped_when_no_renderer(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._LAST_POLL_TS = 0  # no panel poll ⇒ headless ⇒ no wait, no inline verdict
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
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
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
    art._LAST_POLL_TS = 0
    assert "rendered cleanly" in art.check_artifact.invoke({"artifact_id": aid})


def test_live_theme_repush_is_wired(monkeypatch, tmp_path):
    """App-theme switches re-theme the NESTED artifact frame in place (#1872): base()
    bakes tokens in as literals at render time, so without a push the frame kept the
    stale palette. The shell observes the kit's token rewrite on the root element and
    postMessages fresh tokens; the SHIM applies them without a re-srcdoc (interactive
    artifact state survives)."""
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
    # frame side: the SHIM handles the theme message and updates :root vars in place.
    assert 'protoArtifact:theme' in html
    assert 'st.setProperty(k,String(m.tokens[k]))' in html
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
    html = art._SHELL_HTML
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
    monkeypatch.setattr(art, "_max_preview_bytes", lambda: note_len + 5)  # 5-byte body budget
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
