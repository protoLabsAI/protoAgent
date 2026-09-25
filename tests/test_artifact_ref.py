"""The artifact-ref chip (#3617): the plugin component-kind seam, the artifact tools'
component tail, its validator, and the /refs metadata route the console chip reads.

The shell's `protoArtifact:select` BEHAVIOUR is exercised in a real browser (the e2e
suite + the Chromium harness); here we pin the contract strings it must keep."""

from __future__ import annotations

from pathlib import Path

import pytest

from graph import components
from graph.components import extract_component, strip_component
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import load_plugins
from graph.plugins.registry import PluginRegistry
from tests.test_artifact_plugin import _app, _load


@pytest.fixture(autouse=True)
def _clean_plugin_components():
    """Every test starts (and ends) with no plugin kinds live — the set is module state."""
    components.set_plugin_components(None)
    yield
    components.set_plugin_components(None)


def _live(art):
    """Make the artifact plugin's kind live, the way server/agent_init does after a load."""
    components.set_plugin_components({art._ref.ARTIFACT_REF: art._ref.validate_artifact_ref})


# ── the seam ──────────────────────────────────────────────────────────────────────────


def test_unregistered_plugin_kind_is_not_extracted():
    s = "x\n" + components.encode_component("artifact-ref", {"artifact_id": "a", "version": 1, "kind": "html"})
    assert extract_component(s) is None  # not a core kind, and no plugin registered it


def test_registered_plugin_kind_extracts_only_when_its_validator_passes():
    components.set_plugin_components({"thing-ref": lambda p: None if p.get("id") == "ok" else "bad id"})
    good = extract_component("t " + components.encode_component("thing-ref", {"id": "ok"}))
    assert good == {"component": "thing-ref", "props": {"id": "ok"}}
    assert extract_component("t " + components.encode_component("thing-ref", {"id": "nope"})) is None


def test_a_raising_validator_drops_the_payload_not_the_turn():
    def boom(props):
        raise RuntimeError("plugin bug")

    components.set_plugin_components({"thing-ref": boom})
    assert extract_component("t " + components.encode_component("thing-ref", {})) is None


def test_plugin_kinds_cannot_shadow_or_loosen_core_kinds():
    # A plugin registering `code-ref` with a permissive validator must NOT replace its strict schema.
    components.set_plugin_components({"code-ref": lambda p: None, "table": lambda p: "never"})
    assert components.plugin_component_types() == ()
    assert extract_component("x " + components.encode_component("code-ref", {"project": "r"})) is None
    assert extract_component("x " + components.encode_component("table", {"rows": []})) is not None


def test_set_plugin_components_skips_bad_names_and_non_callables():
    components.set_plugin_components({"Bad Name": lambda p: None, "ok-kind": "not callable", "fine": lambda p: None})
    assert components.plugin_component_types() == ("fine",)
    components.set_plugin_components({})  # a reload with the plugin disabled clears it
    assert components.plugin_component_types() == ()


def test_registry_register_component_refuses_core_invalid_and_duplicate(caplog):
    reg = PluginRegistry("p", Path("/tmp"))

    def v(props):
        return None

    def v2(props):
        return None

    reg.register_component("thing-ref", v)
    reg.register_component("code-ref", v)  # core kind — refused
    reg.register_component("Thing", v)  # not lowercase kebab — refused
    reg.register_component("other", None)  # not callable — refused
    reg.register_component("thing-ref", v2)  # duplicate — first wins
    assert reg.components == {"thing-ref": v}


def test_show_component_never_builds_a_plugin_kind():
    from tools.lg_tools import show_component

    components.set_plugin_components({"artifact-ref": lambda p: None})
    out = show_component.invoke({"component": "artifact-ref", "props": {}})
    assert out.startswith("Error: unknown component")
    assert extract_component(out) is None


_KIND_PLUGIN = """
def register(registry):
    registry.register_component("thing-ref", lambda props: None)
"""


def _make_plugin(root: Path, pid: str, body: str) -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid}\nversion: 0.1.0\nenabled: true\n", encoding="utf-8"
    )
    (d / "__init__.py").write_text(body, encoding="utf-8")


def test_loader_collects_plugin_components_first_wins(tmp_path, monkeypatch):
    from graph.config import LangGraphConfig

    root = tmp_path / "plugins"
    _make_plugin(root, "ap", _KIND_PLUGIN)
    _make_plugin(root, "bp", _KIND_PLUGIN)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(LangGraphConfig())
    assert set(res.components) == {"thing-ref"}
    metas = {m["id"]: m for m in res.meta}
    assert metas["ap"]["components"] == ["thing-ref"]


def test_agent_init_applies_and_clears_plugin_components():
    from types import SimpleNamespace

    from server.agent_init import _apply_plugin_registries

    empty = dict(goal_verifiers={}, goal_hooks=[], watch_hooks=[], lifecycle_hooks=[])
    _apply_plugin_registries(SimpleNamespace(**empty, components={"thing-ref": lambda p: None}))
    assert components.plugin_component_types() == ("thing-ref",)
    _apply_plugin_registries(SimpleNamespace(**empty))  # a bundle without the field → cleared
    assert components.plugin_component_types() == ()


# ── the artifact plugin's chip ──────────────────────────────────────────────────────────


def test_register_contributes_the_artifact_ref_kind(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    reg = PluginRegistry("artifact", Path(art.__file__).parent)
    art.register(reg)
    assert set(reg.components) == {"artifact-ref"}
    assert art._ref.EMIT is True


def test_register_on_a_host_without_the_seam_turns_the_tail_off(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)

    class OldRegistry:
        def __getattr__(self, name):
            if name == "register_component":
                raise AttributeError(name)
            return lambda *a, **k: None

    art.register(OldRegistry())
    assert art._ref.EMIT is False
    out = art.show_artifact.invoke({"kind": "html", "code": "<p/>"})
    assert "\x1e" not in out  # no raw sentinel in a card the host can't lift it out of


def test_every_create_and_revise_tool_emits_a_valid_ref(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _live(art)
    out = art.show_artifact.invoke({"kind": "html", "code": "<h1>Hi</h1>", "title": "  My\n  page "})
    comp = extract_component(out)
    aid = art._read_store()["artifacts"][0]["id"]
    assert comp == {
        "component": "artifact-ref",
        "props": {"artifact_id": aid, "version": 1, "versions_total": 1, "title": "My page", "kind": "html"},
    }
    # The model/card text is unchanged — the tail sits after it and strips cleanly.
    assert strip_component(out).startswith(f"Created html artifact {aid}")

    out = art.update_artifact.invoke({"old_string": "Hi", "new_string": "Yo"})
    assert strip_component(out) == f"Updated artifact {aid} → version 2."
    assert extract_component(out)["props"]["version"] == 2

    out = art.rewrite_artifact.invoke({"code": "<h1>New</h1>", "title": "Renamed"})
    props = extract_component(out)["props"]
    assert props["version"] == 3 and props["title"] == "Renamed"

    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    out = art.save_file_artifact.invoke({"path": str(f)})
    props = extract_component(out)["props"]
    assert props["kind"] == "file" and props["version"] == 1 and props["title"] == "notes.txt"
    assert strip_component(out).startswith("Saved file artifact ")


def test_refusals_carry_no_chip(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _live(art)
    assert extract_component(art.update_artifact.invoke({"old_string": "x", "new_string": "y"})) is None
    assert extract_component(art.show_artifact.invoke({"kind": "gif", "code": "x"})) is None
    art.show_artifact.invoke({"kind": "html", "code": "<p/>"})
    assert extract_component(art.update_artifact.invoke({"old_string": "nope", "new_string": "y"})) is None


def test_ref_version_is_the_lifetime_number_past_the_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", "2")
    art = _load(monkeypatch, tmp_path)
    _live(art)
    art.show_artifact.invoke({"kind": "html", "code": "v1"})
    for i in range(2, 5):
        out = art.rewrite_artifact.invoke({"code": f"v{i}"})
    # The model text reports the POSITION (2 — only two kept); the chip names the version for good.
    assert "→ version 2." in strip_component(out)
    assert extract_component(out)["props"]["version"] == 4


def test_long_titles_are_clipped_to_the_validator_bound(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _live(art)
    out = art.show_artifact.invoke({"kind": "svg", "code": "<svg/>", "title": "t" * 500})
    title = extract_component(out)["props"]["title"]
    assert len(title) == art._ref.TITLE_MAX and title.endswith("…")


@pytest.mark.parametrize(
    "bad",
    [
        {"artifact_id": "", "version": 1, "kind": "html"},
        {"artifact_id": "a" * 65, "version": 1, "kind": "html"},
        {"artifact_id": 7, "version": 1, "kind": "html"},
        {"artifact_id": "a", "version": 0, "kind": "html"},
        {"artifact_id": "a", "version": True, "kind": "html"},
        {"artifact_id": "a", "version": "2", "kind": "html"},
        {"artifact_id": "a", "version": 3, "versions_total": 2, "kind": "html"},
        {"artifact_id": "a", "version": 1, "kind": "exe"},
        {"artifact_id": "a", "version": 1, "kind": "html", "title": "x" * 201},
        {"artifact_id": "a", "version": 1, "kind": "html", "title": 5},
        {"artifact_id": "a", "version": 1, "kind": "html", "code": "<script>"},  # never content
    ],
)
def test_validator_rejects_bad_props(monkeypatch, tmp_path, bad):
    art = _load(monkeypatch, tmp_path)
    _live(art)
    assert art._ref.validate_artifact_ref(bad) is not None
    assert extract_component("x " + components.encode_component("artifact-ref", bad)) is None


def test_validator_accepts_minimal_props(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert art._ref.validate_artifact_ref({"artifact_id": "a", "version": 2, "kind": "file"}) is None


# ── /refs ───────────────────────────────────────────────────────────────────────────────


def test_refs_route_reports_counts_and_omits_missing(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", "2")
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "v1", "title": "Doc"})
    for i in range(2, 5):
        art.rewrite_artifact.invoke({"code": f"v{i}"})
    aid = art._read_store()["artifacts"][0]["id"]
    polled = []
    monkeypatch.setattr(art._render_status, "_note_poll", lambda: polled.append(1))
    c = TestClient(_app(art))
    r = c.get("/api/plugins/artifact/refs", params={"ids": f"{aid},gone,{aid}"})
    assert r.status_code == 200
    assert r.json() == {"artifacts": {aid: {"title": "Doc", "kind": "html", "version_count": 4, "oldest": 3}}}
    assert polled == []  # a transcript chip is not a live renderer (#1458's wait stays honest)
    assert c.get("/api/plugins/artifact/refs").json() == {"artifacts": {}}


# ── the shell's deep-link contract ──────────────────────────────────────────────────────


def test_shell_select_is_embedder_only_and_versioned_by_lifetime_number(monkeypatch, tmp_path):
    js = _load(monkeypatch, tmp_path)._SHELL_JS
    assert 'm.type!=="protoArtifact:select" || !fromEmbedder(e)' in js
    # Only the embedding window — never the nested artifact frame (model code) or self.
    assert "window.parent===window || e.source!==window.parent" in js
    assert "e.origin===anc[0]" in js
    # Lifetime numbering, older version pins (auto-follow off), newest follows.
    assert "idx=want-(vtot-a.versions.length)-1" in js
    assert "selVer=Math.max(0,idx); followNewest=false;" in js
    # A request that can't resolve after a FRESH poll (deleted/evicted) is dropped.
    assert "if(!a){ if(fresh) pendingSel=null; return; }" in js
    assert '" of "+vtot' in js
