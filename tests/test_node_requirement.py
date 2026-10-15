"""Plugins can declare a Node runtime requirement; version-aware node resolution (#4238).

ADR 0085 (amended 2026-10-15): one resolver (``infra.node_runtime.resolve_node``) shared by
the ACP client, ``sdk.node_executable`` and the plugin deps gate; a version-aware PATH seam;
the ``requires_runtimes`` manifest field and its ``runtime-missing`` setup gap with an
``install_node`` action.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from graph.plugins import loader, setup_gaps
from graph.plugins.manifest import _parse_requires_runtimes, load_manifest
from infra import node_runtime as nr
from infra.paths import reset_instance_paths

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A node-free box: PROTOAGENT_BOX_ROOT under tmp, an empty PATH, no discovered dirs,
    and a clean version cache — the host's real Node never leaks in."""
    box_root = tmp_path / "box"
    box_root.mkdir()
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(box_root))
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv(nr.NODE_SHADOW_ENV, raising=False)
    monkeypatch.setattr(nr, "discovered_node_dirs", lambda: ())
    monkeypatch.setattr(nr, "_SHADOW_LOGGED", False)
    nr._reset_version_cache()
    reset_instance_paths()
    yield box_root
    nr._reset_version_cache()


def _fake_node(bin_dir: Path) -> Path:
    """An executable file named like node. Its version comes from the ``versions`` stub
    (see ``_stub_versions``), so these tests never depend on executing a shell script."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / nr._node_exe_name()
    exe.write_text("#!/bin/sh\necho v0.0.0\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def _stub_versions(monkeypatch, versions: dict[Path, tuple[int, int, int] | None]) -> list[str]:
    """Answer ``node_version`` from a {path: version} table; record every probe."""
    # Keyed like the code compares paths: on Windows ``which`` returns ``…\\node.EXE`` (the
    # PATHEXT spelling) for the ``node.exe`` this fixture wrote.
    table = {nr._same_path_key(p): v for p, v in versions.items()}
    probed: list[str] = []

    def _fake(exe):
        path = nr._same_path_key(exe)
        probed.append(path)
        return table.get(path)

    monkeypatch.setattr(nr, "node_version", _fake)
    return probed


def _managed_bin() -> Path:
    return nr._bin_dir_for(nr.managed_node_install_dir())


# ── version parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [("v22.11.0", (22, 11, 0)), ("20", (20, 0, 0)), ("v20.6", (20, 6, 0)), ("v24.18.0\n", (24, 18, 0)), ("", None), ("node", None)],
)
def test_parse_node_version(text, expected):
    assert nr.parse_node_version(text) == expected


def test_parse_min_version_accepts_floors_and_rejects_the_rest():
    assert nr.parse_min_version(None) is None
    assert nr.parse_min_version("20") == (20, 0, 0)
    assert nr.parse_min_version(">=20.6.1") == (20, 6, 1)
    assert nr.parse_min_version("v22") == (22, 0, 0)
    for bad in ("^20", "<22", "20.x", "latest", "20.1.2.3", ""):
        with pytest.raises(ValueError):
            nr.parse_min_version(bad)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell-script fake node")
def test_node_version_runs_the_binary_and_caches_by_mtime(box, tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "node"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\necho v20.11.1\n", encoding="utf-8")
    exe.chmod(0o755)
    calls = {"n": 0}
    real_run = nr.subprocess.run

    def _counting(*a, **k):
        calls["n"] += 1
        return real_run(*a, **k)

    monkeypatch.setattr(nr.subprocess, "run", _counting)
    assert nr.node_version(exe) == (20, 11, 1)
    assert nr.node_version(exe) == (20, 11, 1)
    assert calls["n"] == 1  # cached
    # an upgraded binary at the same path is re-read
    exe.write_text("#!/bin/sh\necho v22.1.0\n", encoding="utf-8")
    os.utime(exe, ns=(exe.stat().st_atime_ns, exe.stat().st_mtime_ns + 10_000_000))
    assert nr.node_version(exe) == (22, 1, 0)
    assert calls["n"] == 2
    assert nr.node_version(tmp_path / "missing" / "node") is None


# ── resolver order + version gating ──────────────────────────────────────────


def test_resolve_prefers_path_when_it_satisfies(box, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    disc = _fake_node(tmp_path / "nvm" / "v22.0.0" / "bin")
    managed = _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(user.parent))
    monkeypatch.setattr(nr, "discovered_node_dirs", lambda: (str(disc.parent),))
    _stub_versions(monkeypatch, {user: (20, 1, 0), disc: (22, 0, 0), managed: (24, 18, 0)})

    hit = nr.resolve_node("20")
    assert hit == nr.NodeResolution(Path(os.path.abspath(user)), (20, 1, 0), "path")
    assert hit.path.is_absolute()
    assert hit.version_str == "v20.1.0"


def test_resolve_skips_an_old_path_node_for_a_discovered_one(box, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    disc = _fake_node(tmp_path / "nvm" / "v22.0.0" / "bin")
    _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(user.parent))
    monkeypatch.setattr(nr, "discovered_node_dirs", lambda: (str(disc.parent),))
    _stub_versions(monkeypatch, {user: (18, 19, 0), disc: (22, 0, 0)})

    hit = nr.resolve_node(">=20")
    assert hit is not None and hit.source == "discovered" and hit.path == Path(os.path.abspath(disc))


def test_resolve_falls_back_to_managed(box, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    disc = _fake_node(tmp_path / "nvm" / "v16.0.0" / "bin")
    managed = _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(user.parent))
    monkeypatch.setattr(nr, "discovered_node_dirs", lambda: (str(disc.parent),))
    _stub_versions(monkeypatch, {user: (18, 0, 0), disc: (16, 0, 0), managed: (24, 18, 0)})

    hit = nr.resolve_node("22.1")
    assert hit is not None and hit.source == "managed" and hit.version == (24, 18, 0)
    # nothing satisfies a floor above every candidate
    assert nr.resolve_node("30") is None
    # no floor → the PATH node, however old
    assert nr.resolve_node().source == "path"


def test_resolve_none_when_nothing_installed(box, monkeypatch):
    _stub_versions(monkeypatch, {})
    assert nr.resolve_node() is None
    assert nr.resolve_node("20") is None


def test_resolve_labels_a_path_hit_inside_the_managed_dir_as_managed(box, monkeypatch):
    managed = _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(managed.parent))
    _stub_versions(monkeypatch, {managed: (24, 18, 0)})
    assert nr.resolve_node("20").source == "managed"


def test_resolve_skips_an_unrunnable_candidate(box, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    managed = _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(user.parent))
    _stub_versions(monkeypatch, {user: None, managed: (24, 18, 0)})
    assert nr.resolve_node().source == "managed"


def test_resolve_rejects_a_malformed_floor(box):
    with pytest.raises(ValueError):
        nr.resolve_node("^20")


# ── version-aware PATH seam ──────────────────────────────────────────────────


def test_augment_prepends_managed_over_an_old_path_node(box, tmp_path, monkeypatch, caplog):
    user = _fake_node(tmp_path / "user")
    managed = _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(user.parent))
    _stub_versions(monkeypatch, {user: (18, 19, 0), managed: (24, 18, 0)})

    with caplog.at_level(logging.INFO, logger="protoagent.infra.node"):
        added = nr.augment_path_with_managed_node()
    assert added == str(managed.parent)
    assert os.environ["PATH"].split(os.pathsep) == [str(managed.parent), str(user.parent)]
    assert sum("older than core's floor" in r.getMessage() for r in caplog.records) == 1
    # idempotent — the managed node now wins resolution
    assert nr.augment_path_with_managed_node() is None
    assert os.environ["PATH"].split(os.pathsep) == [str(managed.parent), str(user.parent)]


def test_augment_moves_an_appended_managed_dir_to_the_front(box, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    managed = _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", os.pathsep.join([str(user.parent), str(managed.parent)]))
    _stub_versions(monkeypatch, {user: (16, 0, 0), managed: (24, 18, 0)})
    assert nr.augment_path_with_managed_node() == str(managed.parent)
    assert os.environ["PATH"].split(os.pathsep) == [str(managed.parent), str(user.parent)]


def test_augment_shadow_opt_out_keeps_the_old_path_node_but_still_logs(box, tmp_path, monkeypatch, caplog):
    user = _fake_node(tmp_path / "user")
    managed = _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(user.parent))
    monkeypatch.setenv(nr.NODE_SHADOW_ENV, "0")
    _stub_versions(monkeypatch, {user: (18, 19, 0), managed: (24, 18, 0)})
    with caplog.at_level(logging.INFO, logger="protoagent.infra.node"):
        assert nr.augment_path_with_managed_node() is None
        assert nr.augment_path_with_managed_node() is None
    assert os.environ["PATH"] == str(user.parent)
    assert sum("NOT shadowing" in r.getMessage() for r in caplog.records) == 1
    # any other value keeps the default behaviour
    monkeypatch.setenv(nr.NODE_SHADOW_ENV, "1")
    assert nr.augment_path_with_managed_node() == str(managed.parent)


def test_node_version_probe_hides_the_console_window_on_windows(box, tmp_path, monkeypatch):
    exe = _fake_node(tmp_path / "w")
    seen: dict = {}

    class _Out:
        returncode, stdout = 0, "v22.0.0\n"

    def _run(cmd, **kw):
        seen.update(kw)
        return _Out()

    monkeypatch.setattr(nr.subprocess, "run", _run)
    monkeypatch.setattr(nr.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(nr.os, "name", "nt")
    assert nr.node_version(exe) == (22, 0, 0)
    assert seen.get("creationflags") == 0x08000000
    monkeypatch.setattr(nr.os, "name", "posix")
    nr._reset_version_cache()
    seen.clear()
    nr.node_version(exe)
    assert "creationflags" not in seen


def test_augment_keeps_a_path_node_that_meets_the_floor(box, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(user.parent))
    _stub_versions(monkeypatch, {user: (int(nr.CORE_NODE_FLOOR), 0, 0)})
    assert nr.augment_path_with_managed_node() is None
    assert os.environ["PATH"] == str(user.parent)


def test_augment_leaves_an_unreadable_path_node_alone(box, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    _fake_node(_managed_bin())
    monkeypatch.setenv("PATH", str(user.parent))
    _stub_versions(monkeypatch, {user: None})
    assert nr.augment_path_with_managed_node() is None
    assert os.environ["PATH"] == str(user.parent)


def test_augment_never_prepends_without_a_managed_install(box, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    monkeypatch.setenv("PATH", str(user.parent))
    _stub_versions(monkeypatch, {user: (16, 0, 0)})
    assert nr.augment_path_with_managed_node() is None
    assert os.environ["PATH"] == str(user.parent)


# ── manifest: requires_runtimes ──────────────────────────────────────────────


def test_requires_runtimes_parses_valid_floors():
    assert _parse_requires_runtimes({"node": ">=20"}, "p") == {"node": ">=20"}
    assert _parse_requires_runtimes({"node": ">= 20.6.1"}, "p") == {"node": ">=20.6.1"}
    assert _parse_requires_runtimes({"Node": ">=22.1"}, "p") == {"node": ">=22.1"}
    assert _parse_requires_runtimes(None, "p") == {}


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("20", ">=20"), (20, ">=20"), ("v20", ">=20"), ("v20.6.1", ">=20.6.1"), (">=v22", ">=22"), (" 20.1 ", ">=20.1")],
)
def test_requires_runtimes_normalizes_a_bare_floor(spec, expected):
    """A bare floor means the same as ``sdk.node_executable("20")``: at least that version."""
    assert _parse_requires_runtimes({"node": spec}, "p") == {"node": expected}


@pytest.mark.parametrize("spec", ["^20", "<22", ">=20.x", ">=20.1.2.3", "20.x", "latest", "", None, True, 20.5, -1])
def test_requires_runtimes_drops_a_malformed_spec(spec, caplog):
    with caplog.at_level(logging.WARNING):
        assert _parse_requires_runtimes({"node": spec}, "p") == {}
    assert "requires_runtimes.node" in caplog.text


def test_requires_runtimes_warns_on_unknown_runtimes_and_non_mappings(caplog):
    with caplog.at_level(logging.WARNING):
        assert _parse_requires_runtimes({"deno": ">=1", "node": ">=20"}, "p") == {"node": ">=20"}
        assert _parse_requires_runtimes(["node>=20"], "p") == {}
    assert "unknown runtime 'deno'" in caplog.text
    assert "must be a mapping" in caplog.text


def _plugin(tmp_path: Path, extra: str = 'requires_runtimes:\n  node: ">=20"\n'):
    d = tmp_path / "atlas"
    d.mkdir(exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(f"id: atlas\nname: Atlas\nversion: 0.1.0\n{extra}", encoding="utf-8")
    return load_manifest(d)


def test_manifest_loads_requires_runtimes(tmp_path):
    assert _plugin(tmp_path).requires_runtimes == {"node": ">=20"}
    assert _plugin(tmp_path, "").requires_runtimes == {}


# ── deps gate: the runtime-missing gap + install_node action ─────────────────


@pytest.fixture
def gaps():
    setup_gaps.reset()
    yield
    setup_gaps.reset()


def _gap(plugin="atlas"):
    return next((g for g in setup_gaps.active() if g["plugin"] == plugin and g["key"] == loader.RUNTIME_GAP_KEY), None)


def test_unmet_node_requirement_raises_the_banner_with_an_install_action(box, gaps, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    monkeypatch.setenv("PATH", str(user.parent))
    _stub_versions(monkeypatch, {user: (18, 19, 0)})
    monkeypatch.setattr("runtime.node_install.is_supported", lambda: True)

    assert loader._report_runtime_gap(_plugin(tmp_path)) == ["node>=20"]
    gap = _gap()
    assert gap is not None and gap["label"] == "Atlas"
    assert "needs Node.js >=20 (found v18.19.0)" in gap["message"]
    assert "protoagent runtime install-node" in gap["message"]
    assert gap["actions"] == [{"kind": "install_node", "target": "atlas", "label": "Install Node runtime"}]


def test_no_install_action_when_the_managed_build_cant_help(box, gaps, tmp_path, monkeypatch):
    _stub_versions(monkeypatch, {})
    monkeypatch.setattr("runtime.node_install.is_supported", lambda: False)
    loader._report_runtime_gap(_plugin(tmp_path))
    gap = _gap()
    assert "none found" in gap["message"] and "yourself" in gap["message"]
    assert "actions" not in gap
    # a floor above the pinned managed version can't be met by installing it either
    setup_gaps.reset()
    monkeypatch.setattr("runtime.node_install.is_supported", lambda: True)
    loader._report_runtime_gap(_plugin(tmp_path, 'requires_runtimes:\n  node: ">=99"\n'))
    assert "actions" not in _gap()


def test_met_requirement_clears_the_gap_and_no_requirement_reports_nothing(box, gaps, tmp_path, monkeypatch):
    user = _fake_node(tmp_path / "user")
    monkeypatch.setenv("PATH", str(user.parent))
    versions = {user: (18, 0, 0)}
    _stub_versions(monkeypatch, versions)
    manifest = _plugin(tmp_path)
    loader._report_runtime_gap(manifest)
    assert _gap() is not None
    _stub_versions(monkeypatch, {user: (22, 0, 0)})
    assert loader._report_runtime_gap(manifest) == []
    assert _gap() is None
    assert loader._report_runtime_gap(_plugin(tmp_path, "")) == []
    assert setup_gaps.active() == []


def test_refresh_runtime_gaps_clears_the_banner_after_a_managed_install(box, gaps, tmp_path, monkeypatch):
    _stub_versions(monkeypatch, {})
    manifest = _plugin(tmp_path)
    loader._report_runtime_gap(manifest)
    assert _gap() is not None
    monkeypatch.setattr("graph.plugins.installer.effective_copies", lambda: {"atlas": manifest})
    # still unmet → re-reported, nothing cleared
    assert loader.refresh_runtime_gaps() == []
    assert _gap() is not None
    # the managed install lands
    managed = _fake_node(_managed_bin())
    _stub_versions(monkeypatch, {managed: (24, 18, 0)})
    assert loader.refresh_runtime_gaps() == ["atlas"]
    assert _gap() is None


def test_refresh_drops_the_report_of_a_plugin_disabled_mid_refresh(box, gaps, tmp_path, monkeypatch):
    """The plugin is cleared AFTER the refresh snapshotted its gap but BEFORE it re-reports:
    the generation pinned at snapshot time no longer matches, so the banner stays gone."""
    _stub_versions(monkeypatch, {})
    manifest = _plugin(tmp_path)
    loader._report_runtime_gap(manifest)
    assert _gap() is not None

    def _copies_while_disabling():
        setup_gaps.clear_plugin("atlas")  # the operator disables it right now
        return {"atlas": manifest}

    monkeypatch.setattr("graph.plugins.installer.effective_copies", _copies_while_disabling)
    assert loader.refresh_runtime_gaps() == []
    assert _gap() is None


def test_unmet_requirement_message_says_a_cli_install_needs_a_restart(box, gaps, tmp_path, monkeypatch):
    _stub_versions(monkeypatch, {})
    monkeypatch.setattr("runtime.node_install.is_supported", lambda: True)
    loader._report_runtime_gap(_plugin(tmp_path))
    assert "`protoagent runtime install-node` and then restart protoAgent (or reload the plugin)" in _gap()["message"]


def test_refresh_never_resurrects_a_cleared_plugin(box, gaps, tmp_path, monkeypatch):
    _stub_versions(monkeypatch, {})
    manifest = _plugin(tmp_path)
    loader._report_runtime_gap(manifest)
    setup_gaps.clear_plugin("atlas")  # disabled
    monkeypatch.setattr("graph.plugins.installer.effective_copies", lambda: {"atlas": manifest})
    loader.refresh_runtime_gaps()
    assert _gap() is None


def test_install_node_action_target_is_forced_to_the_reporting_plugin(gaps):
    setup_gaps.report("atlas", "k", "msg", action={"kind": "install_node", "target": "other", "label": "Go"})
    assert setup_gaps.active()[0]["actions"] == [{"kind": "install_node", "target": "atlas", "label": "Go"}]


def test_install_route_refreshes_runtime_gaps(monkeypatch):
    from operator_api import node_routes

    calls: list[str] = []
    monkeypatch.setattr("runtime.node_install.install_managed_node", lambda **_: calls.append("install") or {})
    monkeypatch.setattr("graph.plugins.loader.refresh_runtime_gaps", lambda: calls.append("refresh") or ["atlas"])
    monkeypatch.setitem(node_routes._install, "state", "running")
    node_routes._run_install(False)
    assert calls == ["install", "refresh"]
    assert node_routes._install["state"] == "done"


def test_install_route_survives_a_refresh_failure(monkeypatch):
    from operator_api import node_routes

    def _boom():
        raise RuntimeError("nope")

    monkeypatch.setattr("runtime.node_install.install_managed_node", lambda **_: {})
    monkeypatch.setattr("graph.plugins.loader.refresh_runtime_gaps", _boom)
    monkeypatch.setitem(node_routes._install, "state", "running")
    node_routes._run_install(False)
    assert node_routes._install["state"] == "done"


# ── SDK ──────────────────────────────────────────────────────────────────────


def test_sdk_node_executable(box, tmp_path, monkeypatch):
    from graph import sdk

    user = _fake_node(tmp_path / "user")
    monkeypatch.setenv("PATH", str(user.parent))
    _stub_versions(monkeypatch, {user: (20, 5, 0)})
    assert sdk.node_executable() == Path(os.path.abspath(user))
    assert sdk.node_executable("20") == Path(os.path.abspath(user))
    assert sdk.node_executable(">=20.5") == Path(os.path.abspath(user))
    assert sdk.node_executable("22") is None
    assert sdk.node_executable("not-a-version") is None  # never raises


# ── the ACP client shares the resolver's discovery ───────────────────────────


def test_acp_client_uses_the_shared_discovery():
    pytest.importorskip("plugins.coding_agent.acp_client")
    from plugins.coding_agent import acp_client

    assert acp_client._discovered_node_dirs is nr.discovered_node_dirs
    assert acp_client._version_sort_key is nr._version_sort_key
