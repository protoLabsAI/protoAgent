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
        "My Cool Plugin", summary="demo", with_view=True, with_skill=True,
        with_workflow=True, target_dir=str(tmp_path),
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
    assert "/_ds/plugin-kit.css" in init and "split('/plugins/')" in init  # rules 3 + 4


def test_scaffold_with_tests_is_shippable(tmp_path):
    """with_tests writes a host-free suite + CI + dev deps + pyproject, version-coherent."""
    import yaml

    res = scaffold.scaffold_plugin("Shippable One", with_tests=True, target_dir=str(tmp_path))
    pdir = tmp_path / "shippable-one"
    for rel in (
        "tests/conftest.py",
        "tests/test_shippable_one.py",
        ".github/workflows/ci.yml",
        "requirements-dev.txt",
        "pyproject.toml",
    ):
        assert (pdir / rel).exists(), rel
    assert "tests/" in res.made
    # the generated test file is valid Python
    compile((pdir / "tests" / "test_shippable_one.py").read_text(), "t.py", "exec")
    # manifest ↔ pyproject versions agree (the coherence a release should hold)
    mv = yaml.safe_load((pdir / "protoagent.plugin.yaml").read_text())["version"]
    assert f'version = "{mv}"' in (pdir / "pyproject.toml").read_text()
    # ci.yml is valid YAML with a test job
    ci = yaml.safe_load((pdir / ".github" / "workflows" / "ci.yml").read_text())
    assert "test" in ci["jobs"]


def test_scaffold_comms_plugin(tmp_path):
    res = scaffold.scaffold_plugin("My Chat", with_comms=True, target_dir=str(tmp_path))
    assert res.kind == "comms"
    init = (tmp_path / "my-chat" / "__init__.py").read_text()
    assert "register_chat_surface" in init and "class MyChatAdapter" in init


def test_scaffold_bundle_round_trips_through_loader(tmp_path):
    res = scaffold.scaffold_bundle(
        "Project Manager Stack", summary="board + browser",
        members=[
            {"id": "delegates", "builtin": True},
            {"id": "project_board", "url": "https://github.com/you/pb", "ref": "v0.1.0"},
        ],
        target_dir=str(tmp_path),
    )
    assert res.kind == "bundle" and res.id == "project-manager-stack"
    bdir = tmp_path / "project-manager-stack"
    bundle = installer.load_bundle(bdir)
    assert bundle is not None
    assert bundle["id"] == "project-manager-stack"
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
    rc = run_plugin_cli([
        "new-bundle", "My Stack", "--dir", str(tmp_path),
        "--member", "board=https://github.com/you/board@v1.0.0",
        "--builtin", "delegates",
    ])
    assert rc == 0
    bundle = installer.load_bundle(tmp_path / "my-stack")
    assert {p["id"] for p in bundle["plugins"]} == {"board", "delegates"}
    assert "scaffolded bundle 'my-stack'" in capsys.readouterr().out


def test_cli_new_rejects_bad_member(tmp_path, capsys):
    rc = run_plugin_cli(["new-bundle", "Bad", "--dir", str(tmp_path), "--member", "noequals"])
    assert rc == 1
    assert "bad --member" in capsys.readouterr().err
