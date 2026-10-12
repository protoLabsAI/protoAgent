"""Asset and version resolution across source, wheel and frozen layouts."""

import sys

import pytest

from infra import paths

pytestmark = pytest.mark.platform_sensitive


@pytest.mark.parametrize("layout", ["flat", "src", "wheel", "frozen"])
def test_assets_and_version_share_the_deployment_root(layout, tmp_path, monkeypatch):
    import server
    from runtime import python_install

    root = tmp_path / "deployment"
    import_root = root / "src" if layout == "src" else root
    (import_root / "infra").mkdir(parents=True)
    monkeypatch.setattr(paths, "__file__", str(import_root / "infra" / "paths.py"))
    monkeypatch.setattr(sys, "frozen", layout == "frozen", raising=False)
    if layout == "frozen":
        root = tmp_path / "extracted"
        root.mkdir()
        monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    if layout != "wheel":
        (root / "pyproject.toml").write_text(
            '[project]\nname = "protolabs-agent"\nversion = "9.8.7"\n', encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    assert paths._app_root() == root
    assert server._bundle_root() == root
    assert paths._anchored_pyproject_version() == (None if layout == "wheel" else "9.8.7")

    # The managed Python installer must find the same source/frozen data tree.
    relative = "sidecar/requirements-docs.txt" if layout == "frozen" else "apps/desktop/sidecar/requirements-docs.txt"
    baseline = root / relative
    baseline.parent.mkdir(parents=True)
    baseline.write_text("python-docx\n", encoding="utf-8")
    assert python_install._baseline_requirements_path() == baseline


@pytest.mark.parametrize("manifest", [None, "invalid TOML [", '[project]\nname = "unrelated"\nversion = "9.9.9"\n'])
def test_src_anchor_does_not_adopt_an_unrelated_parent(manifest, tmp_path, monkeypatch):
    source = tmp_path / "src"
    (source / "infra").mkdir(parents=True)
    if manifest is not None:
        (tmp_path / "pyproject.toml").write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(paths, "__file__", str(source / "infra" / "paths.py"))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths._app_root() == source
    assert paths._anchored_pyproject_version() is None


def test_source_soul_fallback_uses_shared_asset_root(tmp_path, monkeypatch):
    from graph import prompts

    # Exercise the prompt path through its public builder with a distinct seed.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "SOUL.md").write_text("source-layout-soul-sentinel", encoding="utf-8")
    monkeypatch.setattr(paths, "_app_root", lambda: tmp_path)
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    paths.reset_instance_paths()
    try:
        # Keep the workspace fallback empty so the bundled seed is selected.
        monkeypatch.chdir(tmp_path)
        assert "source-layout-soul-sentinel" in prompts.build_system_prompt(workspace=str(tmp_path / "workspace"))
    finally:
        paths.reset_instance_paths()
