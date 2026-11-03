"""Reject incompatible candidates before replacing a working plugin (#4669)."""

import sys

import pytest

from graph.config import LangGraphConfig
from graph.plugins import installer, loader
from tests.test_plugin_installer import _git, _make_plugin_repo, env  # noqa: F401
from tests.test_plugin_routes import _client, _wire

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def candidate(env, monkeypatch):  # noqa: F811 — imported fixture
    monkeypatch.setattr(loader, "_host_version", lambda: "0.222.0")
    monkeypatch.setattr(installer, "_audit", lambda *a, **kw: None)
    repo = _make_plugin_repo(env, pid="protobanana", tag="v0.1.0")
    manifest = repo / "protoagent.plugin.yaml"
    manifest.write_text(manifest.read_text().replace("0.1.0", "0.5.0") + "min_protoagent_version: 0.223.0\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "new release")
    _git(repo, "tag", "v0.5.0")
    return repo


@pytest.mark.parametrize("operation", ["update", "install", "force_install"])
def test_incompatible_candidate_preserves_live_copy(candidate, monkeypatch, operation):
    if operation != "install":
        installer.install(str(candidate), "v0.1.0")
    root = installer.live_plugins_dir()
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} if root.exists() else {}
    lock = installer.lock_path()
    old_lock = lock.read_bytes() if lock.exists() else None
    captured = _wire(monkeypatch, enabled=["protobanana"], disabled=[], meta=[])
    monkeypatch.setattr(installer, "check_plugin_update", lambda entry: {"latest_ref": "v0.5.0"})
    if operation == "update":
        response = _client().post("/api/plugins/protobanana/update")
    else:
        response = _client().post("/api/plugins/install", json={"url": str(candidate), "ref": "v0.5.0", "force": True})
    body = response.json()
    assert response.status_code == 400
    assert body["ok"] is False and body["reloaded"] is False
    assert body["error"] == "protobanana 0.5.0 requires protoAgent >= 0.223.0 but this host is 0.222.0"
    assert body["detail"] == body["error"]  # console's HTTP error renderer
    assert captured == {}  # no activation/reload on refusal
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert (lock.read_bytes() if lock.exists() else None) == old_lock
    if operation != "install":
        monkeypatch.setattr(loader, "_plugin_roots", lambda cfg: [root])
        loaded = loader.load_plugins(LangGraphConfig(plugins_enabled=["protobanana"]))
        meta = next(m for m in loaded.meta if m["id"] == "protobanana")
        assert meta["loaded"], meta.get("error")
        loader.purge_plugin_modules("protobanana")


@pytest.mark.parametrize("operation", ["update", "install"])
@pytest.mark.parametrize("broken", [False, True])
def test_compatible_candidate_reports_actual_load_result(candidate, monkeypatch, operation, broken):
    installer.install(str(candidate), "v0.1.0")
    monkeypatch.setattr(loader, "_host_version", lambda: "0.223.0")
    _wire(monkeypatch, enabled=["protobanana"], disabled=[], meta=[])
    monkeypatch.setattr(installer, "check_plugin_update", lambda entry: {"latest_ref": "v0.5.0"})
    monkeypatch.setattr(loader, "_plugin_roots", lambda cfg: [installer.live_plugins_dir()])
    if broken:
        (candidate / "__init__.py").write_text('raise RuntimeError("registration exploded")\n')
        _git(candidate, "add", "-A")
        _git(candidate, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "broken")
        _git(candidate, "tag", "-f", "v0.5.0")

    def reload(config=None):
        from runtime.state import STATE

        result = loader.load_plugins(LangGraphConfig(plugins_enabled=["protobanana"]))
        monkeypatch.setattr(STATE, "plugin_meta", result.meta)
        return True, ["graph rebuilt"]

    monkeypatch.setattr(sys.modules["server.agent_init"], "_apply_settings_changes", reload)
    try:
        if operation == "update":
            response = _client().post("/api/plugins/protobanana/update")
        else:
            response = _client().post("/api/plugins/install", json={"url": str(candidate), "ref": "v0.5.0", "force": True})
        body = response.json()
        assert body["ok"] is (not broken)
        assert body["reloaded"] is (not broken)
        assert installer.load_manifest(installer.live_plugins_dir() / "protobanana").version == "0.5.0"
        if broken:
            assert "registration exploded" in body["load_errors"]["protobanana"]
            assert "registration exploded" in body["error"]
        else:
            assert body["load_errors"] == {}
    finally:
        loader.purge_plugin_modules("protobanana")


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("compatible_pin", [False, True])
def test_bundle_host_preflight(candidate, env, monkeypatch, existing, compatible_pin):  # noqa: F811
    """Chased releases fall back; an incompatible floor cannot leave a partial bundle."""
    from tests.test_plugin_installer import _make_bundle_repo, _set_bundle_member_refs

    first = _make_plugin_repo(env, pid="first", tag="v0.1.0")
    last = _make_plugin_repo(env, pid="last", tag="v0.1.0")
    # A newer tag within the pin's caret range, but requiring a newer host.
    _git(candidate, "tag", "v0.1.1")
    bundle = _make_bundle_repo(env, [first, candidate, last])
    _set_bundle_member_refs(bundle, "v0.1.0")
    monkeypatch.setattr(installer, "check_plugin_update", lambda entry: {"latest_ref": entry["requested_ref"]})
    if existing:
        installer.install(str(bundle))
    if not compatible_pin:
        import yaml

        manifest = bundle / "protoagent.bundle.yaml"
        data = yaml.safe_load(manifest.read_text())
        data["plugins"][2]["ref"] = "v0.1.1"
        manifest.write_text(yaml.safe_dump(data))
        _git(bundle, "add", "-A")
        _git(bundle, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "incompatible floor")
    monkeypatch.setattr(
        installer, "check_plugin_update",
        lambda entry: {"latest_ref": "v0.1.1" if entry["source_url"] == str(candidate) else "v0.1.0"},
    )
    root = installer.live_plugins_dir()
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    lock = installer.lock_path()
    before_lock = lock.read_bytes() if lock.exists() else None
    if compatible_pin:
        result = installer.install(str(bundle), force=existing)
        assert [s["id"] for s in result["installed"]] == ["first", "protobanana", "last"]
        assert result["installed"][1]["version"] == "0.1.0"
        assert "using bundle pin v0.1.0" in result["warnings"][0]
        assert "requires protoAgent >= 0.223.0" in result["warnings"][0]
        assert installer._read_lock()["bundles"][0]["plugins"] == ["first", "protobanana", "last"]
    else:
        with pytest.raises(installer.IncompatibleHostError, match="requires protoAgent >= 0.223.0"):
            installer.install(str(bundle), force=existing)
        assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        assert (lock.read_bytes() if lock.exists() else None) == before_lock


def test_bundle_installs_the_preflighted_commit(candidate, monkeypatch):
    """A moving tag cannot replace a checked candidate between preflight and install."""
    original_check = installer._check_candidate_host

    def check_then_move(url, ref, allow):
        sha = original_check(url, ref, allow)
        _git(candidate, "tag", "-f", "v0.1.0", "v0.5.0")
        return sha

    monkeypatch.setattr(installer, "_check_candidate_host", check_then_move)
    monkeypatch.setattr(installer, "check_plugin_update", lambda entry: {"latest_ref": "v0.1.0"})
    result = installer._install_bundle(
        {"id": "stack", "plugins": [{"url": str(candidate), "ref": "v0.1.0"}]},
        "bundle-source", "a" * 40, None, force=False, by="test", allow=None,
    )
    assert result["installed"][0]["version"] == "0.1.0"
    entry = installer._read_lock()["plugins"][0]
    assert entry["requested_ref"] == "v0.1.0"
    assert entry["resolved_sha"] == result["installed"][0]["resolved_sha"]
