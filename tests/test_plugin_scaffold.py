"""Core plugin/bundle scaffolding (graph.plugins.scaffold) + the `plugin new`
CLI — the writers shared by the devkit tool and the shell (ADR 0027 / 0040)."""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.plugins import installer, scaffold
from graph.plugins import loader as plugin_loader
from graph.plugins.cli import run_plugin_cli
from graph.plugins.loader import load_plugins


def _cfg(**kw):
    return LangGraphConfig(**kw)


def test_slug():
    assert scaffold.slug("My Cool Plugin!") == "my-cool-plugin"
    assert scaffold.slug("") == "plugin"


def test_scaffold_plugin_is_loadable(monkeypatch, tmp_path):
    res = scaffold.scaffold_plugin(
        "My Cool Plugin",
        summary="demo",
        with_view=True,
        with_skill=True,
        with_workflow=True,
        target_dir=str(tmp_path),
    )
    assert res.id == "my-cool-plugin" and res.kind == "plugin"
    pdir = tmp_path / "my-cool-plugin"
    assert (pdir / "protoagent.plugin.yaml").exists()
    assert (pdir / "__init__.py").exists()
    assert (pdir / "skills").is_dir() and (pdir / "workflows").is_dir()

    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])
    out = load_plugins(_cfg(plugins_enabled=["my-cool-plugin"]))
    meta = next(m for m in out.meta if m["id"] == "my-cool-plugin")
    assert meta["loaded"], meta.get("error")
    assert "my_cool_plugin_hello" in meta["tools"]
    assert meta["routers"] >= 1  # the view router


def test_scaffold_plugin_refuses_overwrite(tmp_path):
    scaffold.scaffold_plugin("dup", target_dir=str(tmp_path))
    with pytest.raises(FileExistsError):
        scaffold.scaffold_plugin("dup", target_dir=str(tmp_path))


def test_scaffold_view_is_served_on_the_public_prefix(tmp_path):
    """The view PAGE must be on the PUBLIC /plugins/<id> prefix (an iframe page-load
    can't carry a bearer) — not the gated /api/plugins/<id> — and follow the four
    rules (slug-aware base + DS kit)."""
    scaffold.scaffold_plugin("Viewy", with_view=True, target_dir=str(tmp_path))
    pdir = tmp_path / "viewy"
    init = (pdir / "__init__.py").read_text()
    manifest = (pdir / "protoagent.plugin.yaml").read_text()
    assert 'prefix="/plugins/viewy"' in init  # public page route, not /api/plugins/viewy
    assert "path: /plugins/viewy/view" in manifest
    # Rules 3 + 4: slug-aware base + BOTH kit assets (css for tokens, js for the handshake).
    assert "/_ds/plugin-kit.css" in init and "/_ds/plugin-kit.js" in init
    assert "split('/plugins/')" in init
    # Two-router pattern: a gated data route under /api/plugins/<id> the page fetches.
    assert 'prefix="/api/plugins/viewy"' in init


@pytest.mark.parametrize("variant", ["default", "view", "comms"])
def test_scaffold_has_no_future_annotations(tmp_path, variant):
    """Keep annotations eager in every Python template, including the test harness."""
    scaffold.scaffold_plugin(
        "Fresh Plugin",
        with_view=variant == "view",
        with_comms=variant == "comms",
        with_tests=True,
        target_dir=str(tmp_path),
    )
    pdir = tmp_path / "fresh-plugin"
    files = [pdir / "__init__.py"]
    if variant != "comms":
        files += [pdir / "tests/conftest.py", pdir / "tests/test_fresh_plugin.py"]
    # _plugin_testkit.py is copied verbatim, not generated from a scaffold stub.
    for path in files:
        source = path.read_text()
        assert "from __future__ import annotations" not in source, path
        compile(source, str(path), "exec", dont_inherit=True)


def test_scaffold_with_tests_is_shippable(tmp_path):
    """with_tests writes a host-free suite + CI + dev deps + pyproject, version-coherent."""
    import yaml

    res = scaffold.scaffold_plugin("Shippable One", with_tests=True, target_dir=str(tmp_path))
    pdir = tmp_path / "shippable-one"
    for rel in (
        "tests/conftest.py",
        "tests/_plugin_testkit.py",
        "tests/test_shippable_one.py",
        ".github/workflows/ci.yml",
        "requirements-dev.txt",
        "pyproject.toml",
    ):
        assert (pdir / rel).exists(), rel
    assert "tests/" in res.made
    # the vendored testkit is a VERBATIM copy of the canonical source (single source of truth)
    from pathlib import Path as _P

    canonical = (_P(scaffold.__file__).with_name("testkit.py")).read_text()
    assert (pdir / "tests" / "_plugin_testkit.py").read_text() == canonical
    # the generated test file is valid Python
    compile((pdir / "tests" / "test_shippable_one.py").read_text(), "t.py", "exec")
    # manifest ↔ pyproject versions agree (the coherence a release should hold)
    mv = yaml.safe_load((pdir / "protoagent.plugin.yaml").read_text())["version"]
    assert f'version = "{mv}"' in (pdir / "pyproject.toml").read_text()
    # ci.yml is valid YAML with a test job
    ci = yaml.safe_load((pdir / ".github" / "workflows" / "ci.yml").read_text())
    assert "test" in ci["jobs"]
    # ...on a GitHub-hosted runner (the org retired Namespace runners).
    assert ci["jobs"]["test"]["runs-on"] == "ubuntu-latest"


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("with_view", [False, True, "basic", "bus"], ids=["default", "view", "basic", "bus"])
def test_scaffolded_suite_passes_end_to_end(tmp_path, with_view):
    """The generated suite is GREEN out of the box — run pytest on the scaffolded plugin in
    a subprocess (cwd = the plugin, where ``graph`` is absent, so the vendored testkit's
    host stubs are exercised for real, the standalone-plugin path)."""
    import subprocess
    import sys

    scaffold.scaffold_plugin("Green Birth", with_tests=True, with_view=with_view, target_dir=str(tmp_path))
    pdir = tmp_path / "green-birth"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(pdir), capture_output=True, text=True)
    assert proc.returncode == 0, f"scaffolded suite failed:\n{proc.stdout}\n{proc.stderr}"


def test_scaffold_comms_plugin(tmp_path):
    res = scaffold.scaffold_plugin("My Chat", with_comms=True, target_dir=str(tmp_path))
    assert res.kind == "comms"
    init = (tmp_path / "my-chat" / "__init__.py").read_text()
    assert "register_chat_surface" in init and "class MyChatAdapter" in init


def test_scaffold_bundle_round_trips_through_loader(tmp_path):
    res = scaffold.scaffold_bundle(
        "Project Manager Archetype",
        summary="board + browser",
        members=[
            {"id": "delegates", "builtin": True},
            {"id": "project_board", "url": "https://github.com/you/pb", "ref": "v0.1.0"},
        ],
        target_dir=str(tmp_path),
    )
    assert res.kind == "bundle" and res.id == "project-manager-archetype"
    bdir = tmp_path / "project-manager-archetype"
    bundle = installer.load_bundle(bdir)
    assert bundle is not None
    assert bundle["id"] == "project-manager-archetype"
    ids = {p["id"] for p in bundle["plugins"]}
    assert ids == {"delegates", "project_board"}
    assert any(p.get("builtin") for p in bundle["plugins"])
    assert bundle["enabled"] == ["delegates", "project_board"]


def test_scaffold_bundle_placeholder_is_valid_yaml(tmp_path):
    """A member-less bundle still parses (a REPLACE_ME template, not broken YAML)."""
    scaffold.scaffold_bundle("Empty Stack", target_dir=str(tmp_path))
    bundle = installer.load_bundle(tmp_path / "empty-stack")
    assert bundle is not None and bundle["enabled"] == ["REPLACE_ME"]


def test_cli_new_scaffolds(tmp_path, capsys):
    rc = run_plugin_cli(["new", "My Plugin", "--dir", str(tmp_path), "--view"])
    assert rc == 0
    assert (tmp_path / "my-plugin" / "__init__.py").exists()
    assert "scaffolded plugin 'my-plugin'" in capsys.readouterr().out


def test_cli_new_bundle_scaffolds(tmp_path, capsys):
    rc = run_plugin_cli(
        [
            "new-bundle",
            "My Stack",
            "--dir",
            str(tmp_path),
            "--member",
            "board=https://github.com/you/board@v1.0.0",
            "--builtin",
            "delegates",
        ]
    )
    assert rc == 0
    bundle = installer.load_bundle(tmp_path / "my-stack")
    assert {p["id"] for p in bundle["plugins"]} == {"board", "delegates"}
    assert "scaffolded bundle 'my-stack'" in capsys.readouterr().out


def test_cli_new_rejects_bad_member(tmp_path, capsys):
    rc = run_plugin_cli(["new-bundle", "Bad", "--dir", str(tmp_path), "--member", "noequals"])
    assert rc == 1
    assert "bad --member" in capsys.readouterr().err


def test_scaffold_defaults_include_tests_and_usage_skill(tmp_path):
    result = scaffold.scaffold_plugin("Defaults", target_dir=str(tmp_path))
    assert (result.path / "tests/test_defaults.py").is_file()
    skill = (result.path / "skills/defaults-skill/SKILL.md").read_text()
    for heading in ("# Using Defaults", "## When to use it", "## Tool order", "## Failure modes"):
        assert heading in skill


@pytest.mark.parametrize("view", [False, True, "basic"])
def test_explicit_legacy_shapes(tmp_path, view):
    result = scaffold.scaffold_plugin(
        "Legacy", with_view=view, with_tests=False, with_skill=False, target_dir=str(tmp_path)
    )
    assert {p.name for p in result.path.iterdir()} == {"__init__.py", "protoagent.plugin.yaml"}
    source = (result.path / "__init__.py").read_text()
    expected = scaffold._TOOL_STUB.format(id="legacy", id_us="legacy")
    if view:
        expected += scaffold._VIEW_STUB.format(id="legacy", name="Legacy")
    assert source == scaffold._INIT_STUB.format(name="Legacy", view_import="", registrations=expected, id_us="legacy")


def test_bus_scaffold_files_and_topics(tmp_path):
    import yaml

    result = scaffold.scaffold_plugin("Bus Demo", with_view="bus", target_dir=str(tmp_path))
    for rel in (
        "__init__.py",
        "protoagent.plugin.yaml",
        "views/main.html",
        "tests/test_bus_view.py",
        "tests/test_bus_demo.py",
        "skills/bus-demo-skill/SKILL.md",
    ):
        assert (result.path / rel).is_file()
    manifest = yaml.safe_load((result.path / "protoagent.plugin.yaml").read_text())
    source = (result.path / "__init__.py").read_text()
    page = (result.path / "views/main.html").read_text()
    assert manifest["emits"] == ["bus-demo.show", "bus-demo.reply"]
    assert manifest["subscribes"] == ["bus-demo.show", "bus-demo.reply"]
    for topic in manifest["emits"]:
        assert f'registry.emit("{topic}"' in source
    assert 'registry.on("bus-demo.reply"' in source
    assert "kit.subscribe('bus-demo.show'" in page
    assert "kit.publish('bus-demo.reply'" in page
    assert "postMessage" not in page
    assert "background: true" not in page


def test_bus_binary_route_uses_host_operator_gate(tmp_path, monkeypatch):
    from a2a_impl import auth
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from graph.plugins.testkit import FakeRegistry, load_plugin

    result = scaffold.scaffold_plugin("Gated", with_view="bus", target_dir=str(tmp_path))
    registry = FakeRegistry(plugin_id="gated")
    load_plugin(result.path, "gated").register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    # The host adds enabled plugins' public page namespaces when mounting them.
    monkeypatch.setattr(auth, "_PLUGIN_PUBLIC", ["/plugins/gated/"])
    monkeypatch.setattr(auth, "_BEARER", ["operator-secret"])
    monkeypatch.setattr(auth, "_FEDERATION", ["peer-secret"])
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        assert client.get("/plugins/gated/view").status_code == 200
        for token, status in (("", 401), ("peer-secret", 403), ("operator-secret", 200)):
            response = client.post(
                "/api/plugins/gated/reply",
                content=b"png",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "image/png"},
            )
            assert response.status_code == status
    assert len(registry.saved_media) == 1
    assert registry.emitted[-1][0] == "gated.reply"


def test_bus_scaffold_can_omit_tests_and_skill(tmp_path):
    result = scaffold.scaffold_plugin(
        "Minimal Bus",
        with_view="bus",
        with_tool=False,
        with_tests=False,
        with_skill=False,
        target_dir=str(tmp_path),
    )
    assert (result.path / "views/main.html").is_file()
    assert not (result.path / "tests").exists()
    assert not (result.path / "skills").exists()
    source = (result.path / "__init__.py").read_text()
    assert "def minimal_bus_show" in source
    assert "def minimal_bus_hello" not in source


@pytest.mark.platform_sensitive
def test_bus_page_renders_and_publishes_reply(tmp_path):
    import json
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the generated view script")
    result = scaffold.scaffold_plugin("Round Trip", with_view="bus", target_dir=str(tmp_path))
    page = (result.path / "views/main.html").read_text()
    script = re.search(r'<script type="module">(.*?)</script>', page, re.S).group(1)
    # Inject the kit boundary; execute the generated callback and upload helper.
    script = script.replace("await import(window.__base + '/_ds/plugin-kit.js')", "kitStub")
    harness = """
const assert = require('node:assert/strict');
const out = {};
let handler, topic, published, upload, initialized = false;
const kitStub = {
    initPluginView() { initialized = true; },
    subscribe(name, fn) { topic = name; handler = fn; return () => {}; },
    publish(name, data) { published = { name, data }; },
    async apiFetch(path, options) {
        upload = { path, options };
        return { ok: true, json: async () => ({ url: '/media/reply.png' }) };
    }
};
const document = { getElementById(id) { assert.equal(id, 'out'); return out; } };
const window = { addEventListener() {} };
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
(async () => {
    const uploadScreenshot = await new AsyncFunction('kitStub', 'document', 'window',
        SOURCE + '\\nreturn uploadScreenshot;')(kitStub, document, window);
    assert.ok(initialized);
    assert.equal(topic, 'round-trip.show');
    handler({ data: { message: '<b>hello</b>' } });
    assert.equal(out.textContent, '<b>hello</b>');
    assert.deepEqual(published, {
        name: 'round-trip.reply', data: { message: '<b>hello</b>', rendered: true }
    });
    const blob = new Uint8Array([1, 2, 3]);
    assert.deepEqual(await uploadScreenshot(blob), { url: '/media/reply.png' });
    assert.equal(upload.path, '/api/plugins/round-trip/reply');
    assert.equal(upload.options.method, 'POST');
    assert.equal(upload.options.body, blob);
    assert.equal(upload.options.headers['Content-Type'], 'image/png');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    proc = subprocess.run(
        [node, "-e", harness.replace("SOURCE", json.dumps(script))],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
