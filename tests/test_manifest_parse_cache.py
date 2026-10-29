"""Plugin manifests are parsed once per file version (GET /api/config perf).

Discovery runs on request paths: ``GET /api/config`` walks it twice through
``installed_plugin_config_schemas``. Re-parsing every manifest each time held a live
agent's event loop for 100-200 ms per call.
"""

from __future__ import annotations

import yaml

import graph.plugins.manifest as manifest_mod
from graph.plugins.manifest import MANIFEST_FILENAME, load_manifest


def _write(plugin_dir, name: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / MANIFEST_FILENAME).write_text(f"id: cachetest\nname: {name}\nversion: 0.1.0\n", encoding="utf-8")


def _count_parses(monkeypatch) -> list[int]:
    calls = [0]
    real = yaml.load

    def counting(stream, Loader):  # noqa: N803 - mirrors yaml.load's signature
        calls[0] += 1
        return real(stream, Loader=Loader)

    monkeypatch.setattr(manifest_mod.yaml, "load", counting)
    monkeypatch.setattr(manifest_mod, "_PARSED", {})
    return calls


def test_an_unchanged_manifest_is_parsed_once(tmp_path, monkeypatch):
    calls = _count_parses(monkeypatch)
    _write(tmp_path / "p", "First")

    assert load_manifest(tmp_path / "p").name == "First"
    assert load_manifest(tmp_path / "p").name == "First"
    assert calls[0] == 1


def test_an_edited_manifest_is_parsed_again(tmp_path, monkeypatch):
    calls = _count_parses(monkeypatch)
    _write(tmp_path / "p", "First")
    load_manifest(tmp_path / "p")

    _write(tmp_path / "p", "Renamed plugin")

    assert load_manifest(tmp_path / "p").name == "Renamed plugin"
    assert calls[0] == 2


def test_callers_get_a_private_copy_of_the_cached_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest_mod, "_PARSED", {})
    _write(tmp_path / "p", "First")
    path = tmp_path / "p" / MANIFEST_FILENAME

    first = manifest_mod._read_manifest_yaml(path)
    first["name"] = "mutated by a caller"

    assert manifest_mod._read_manifest_yaml(path)["name"] == "First"


def test_a_rewrite_that_leaves_every_stat_field_unchanged_is_seen(tmp_path, monkeypatch):
    """Windows: an in-place same-size save keeps mtime, size, ctime and file index. The
    cache is keyed on content, so it must still serve the new manifest."""
    import os

    _count_parses(monkeypatch)
    _write(tmp_path / "p", "Alpha")
    path = tmp_path / "p" / MANIFEST_FILENAME
    assert load_manifest(tmp_path / "p").name == "Alpha"

    frozen = os.stat(path)
    real_stat = os.stat
    monkeypatch.setattr(
        os, "stat", lambda t, *a, **k: frozen if os.fspath(t) == str(path) else real_stat(t, *a, **k)
    )
    _write(tmp_path / "p", "Omega")  # same length as "Alpha"
    assert os.stat(path) == frozen

    assert load_manifest(tmp_path / "p").name == "Omega"


def test_installed_schemas_resolve_roots_from_the_configured_plugins_dir(monkeypatch):
    from graph.plugins import installer, pconfig

    seen = {}
    monkeypatch.setattr(installer, "configured_plugins_dir", lambda: "/opt/extra-plugins")
    monkeypatch.setattr(pconfig, "plugin_roots_from", lambda live, override: seen.setdefault("o", override) and [])
    monkeypatch.setattr(pconfig, "discover_plugin_config", lambda *a, **k: [])
    pconfig.installed_plugin_config_schemas()
    assert seen["o"] == "/opt/extra-plugins"
