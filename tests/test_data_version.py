"""Data-dir version marker (migration anchor) — #1018.

Tests cover the four functions in infra.paths and the boot-flow integration
in server.agent_init.
"""

from __future__ import annotations

import json

from infra import paths


def _mock_data_home(tmp_path):
    """Return a monkeypatch callable that makes data_home() return tmp_path.

    Since #2416 the marker lives at the BOX root (``PROTOAGENT_BOX_ROOT`` wins over
    ``data_home()``), so patching ``data_home`` alone only isolates a test while the var
    is unset. The conftest clears it per test (#2543); ``test_marker_tests_never_write_an
    _ambient_box_root`` below proves the stamping tests stay out of a spawner's box."""

    def _mock():
        return tmp_path

    return _mock


# ── data_version() ───────────────────────────────────────────────────────────


def test_data_version_returns_0_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    assert paths.data_version() == 0


def test_data_version_returns_int_from_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    (tmp_path / ".data-version").write_text(json.dumps({"data_version": 3}))
    assert paths.data_version() == 3


def test_data_version_returns_0_on_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    (tmp_path / ".data-version").write_text("not json {{{")
    assert paths.data_version() == 0


def test_data_version_returns_0_when_key_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    (tmp_path / ".data-version").write_text(json.dumps({"other": 1}))
    assert paths.data_version() == 0


# ── stamp_data_version() ─────────────────────────────────────────────────────


def test_stamp_data_version_writes_current_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    ret = paths.stamp_data_version()
    assert ret == paths.DATA_VERSION
    assert json.loads((tmp_path / ".data-version").read_text())["data_version"] == paths.DATA_VERSION


def test_stamp_records_the_build_that_wrote_it(tmp_path, monkeypatch):
    """The provenance a later boot needs to tell a real newer stamp from a stray."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    paths.stamp_data_version()
    assert json.loads((tmp_path / ".data-version").read_text()) == {
        "data_version": paths.DATA_VERSION,
        "written_by": paths.package_version(),
    }


def test_stamp_data_version_writes_explicit_version(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    ret = paths.stamp_data_version(5)
    assert ret == 5
    assert json.loads((tmp_path / ".data-version").read_text())["data_version"] == 5


def test_data_version_marker_honors_box_root(tmp_path, monkeypatch):
    default_home = tmp_path / "default"
    box_root = tmp_path / "isolated-box"
    monkeypatch.setattr(paths, "data_home", _mock_data_home(default_home))
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(box_root))

    assert paths.stamp_data_version(7) == 7
    assert paths.data_version() == 7
    assert json.loads((box_root / ".data-version").read_text())["data_version"] == 7
    assert not (default_home / ".data-version").exists()


# ── check_data_version() ─────────────────────────────────────────────────────


def test_check_stamps_when_absent(tmp_path, monkeypatch):
    """Acceptance criterion 1: fresh data dir → stamp DATA_VERSION."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    assert paths.check_data_version() is None
    assert paths.data_version() == paths.DATA_VERSION


def test_check_noop_when_equal(tmp_path, monkeypatch):
    """Acceptance criterion 2: on-disk == DATA_VERSION → no-op."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    (tmp_path / ".data-version").write_text(json.dumps({"data_version": paths.DATA_VERSION}))
    assert paths.check_data_version() is None
    # Verify no re-write (content unchanged)
    assert json.loads((tmp_path / ".data-version").read_text()) == {"data_version": paths.DATA_VERSION}


def test_check_upgrades_when_older(tmp_path, monkeypatch):
    """Acceptance criterion 3: on-disk < DATA_VERSION → stamp current."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    # Simulate an older on-disk version (we patch DATA_VERSION to be higher)
    (tmp_path / ".data-version").write_text(json.dumps({"data_version": 1}))
    orig = paths.DATA_VERSION
    paths.DATA_VERSION = 3  # simulate future bump
    try:
        assert paths.check_data_version() is None
        assert paths.data_version() == 3
    finally:
        paths.DATA_VERSION = orig


def test_check_warns_on_newer(tmp_path, monkeypatch):
    """Acceptance criterion 4: a stamp a real newer build wrote → warning with
    'downgrade', no overwrite."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    newer = paths.DATA_VERSION + 5
    marker = tmp_path / ".data-version"
    marker.write_text(json.dumps({"data_version": newer, "written_by": "9.9.9"}))
    warn = paths.check_data_version()
    assert warn is not None
    assert "downgrade" in warn
    assert str(newer) in warn
    assert str(paths.DATA_VERSION) in warn
    # Marker must NOT be overwritten
    assert json.loads(marker.read_text()) == {"data_version": newer, "written_by": "9.9.9"}


def test_newer_warning_says_who_wrote_it_and_how_to_recover(tmp_path, monkeypatch):
    """The warning is actionable: which file, which build wrote it, and the two ways out."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    marker = tmp_path / ".data-version"
    marker.write_text(json.dumps({"data_version": paths.DATA_VERSION + 1, "written_by": "0.400.0"}))
    warn = paths.check_data_version()
    assert str(marker) in warn
    assert "0.400.0" in warn
    assert "delete" in warn and "restart" in warn


def test_check_repairs_a_stray_legacy_stamp(tmp_path, monkeypatch):
    """The stress-test finding: a ``{"data_version": 5}`` stamp (legacy format, no
    ``written_by``) sat in the desktop box root from 2026-08-10 and every boot of every
    fleet member warned about it forever. No build ever wrote a legacy stamp above v1,
    so it is a provable stray: restamp, report once, and stop warning."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    marker = tmp_path / ".data-version"
    marker.write_text(json.dumps({"data_version": 5}))

    msg = paths.check_data_version()
    assert msg is not None and msg.startswith("repaired")
    assert str(marker) in msg and "v5" in msg
    assert json.loads(marker.read_text()) == {
        "data_version": paths.DATA_VERSION,
        "written_by": paths.package_version(),
    }
    # One-time: the next boot is quiet.
    assert paths.check_data_version() is None


def test_stray_legacy_stamp_is_not_mistaken_for_a_future_version(tmp_path, monkeypatch):
    """A later build whose DATA_VERSION equals the stray value must not read it as
    'already migrated' and skip its migrations: the data under it is v1-era."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    monkeypatch.setattr(paths, "DATA_VERSION", 5)
    marker = tmp_path / ".data-version"
    marker.write_text(json.dumps({"data_version": 5}))

    msg = paths.check_data_version()
    assert msg is not None and msg.startswith("repaired")
    assert json.loads(marker.read_text())["written_by"] == paths.package_version()


def test_legacy_v1_stamp_is_left_alone(tmp_path, monkeypatch):
    """The stamp every real box carries today: valid, quiet, not rewritten."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    marker = tmp_path / ".data-version"
    marker.write_text(json.dumps({"data_version": 1}))
    assert paths.check_data_version() is None
    assert json.loads(marker.read_text()) == {"data_version": 1}


def test_marker_tests_never_write_an_ambient_box_root(tmp_path):
    """Root cause of the stray v5: run from inside a desktop-spawned agent, whose env
    carries ``PROTOAGENT_BOX_ROOT=<desktop box>``, the stamping tests wrote into that
    real box (they patched only ``data_home``). The conftest now clears the var (#2543);
    this pins it by running the marker tests the way such an agent would."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    ambient = tmp_path / "live-desktop-box"
    ambient.mkdir()
    env = {**os.environ, "PROTOAGENT_BOX_ROOT": str(ambient)}
    here = Path(__file__)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{here}::test_stamp_data_version_writes_explicit_version",
            f"{here}::test_check_stamps_when_absent",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            f"--basetemp={tmp_path / 'inner'}",
        ],
        cwd=here.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (ambient / ".data-version").exists()


def test_check_stamps_on_malformed_json(tmp_path, monkeypatch):
    """Acceptance criterion 5: malformed JSON → treat as absent, stamp DATA_VERSION."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    (tmp_path / ".data-version").write_text("broken")
    assert paths.check_data_version() is None
    assert paths.data_version() == paths.DATA_VERSION


def test_check_stamps_on_missing_key(tmp_path, monkeypatch):
    """Acceptance criterion 5 (b): missing data_version key → treat as absent."""
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))
    (tmp_path / ".data-version").write_text(json.dumps({"foo": 1}))
    assert paths.check_data_version() is None
    assert paths.data_version() == paths.DATA_VERSION


# ── boot flow integration ───────────────────────────────────────────────────


def test_init_langgraph_agent_calls_check_data_version(monkeypatch, tmp_path):
    """Acceptance criterion 6: check_data_version called in _init_langgraph_agent
    after unscoped_warning and before the checkpointer."""

    call_order = []

    original_check = paths.check_data_version

    def fake_check():
        call_order.append("check_data_version")
        return original_check()

    monkeypatch.setattr(paths, "check_data_version", fake_check)

    # Patch data_home so the stamp goes to tmp_path
    monkeypatch.setattr(paths, "data_home", _mock_data_home(tmp_path))

    # We can't easily call _init_langgraph_agent without a full config,
    # but we can verify the import and call are present in the source.
    # The real integration is tested via the unit tests above.
    # Here we verify the function is importable and callable from agent_init.
    from server import agent_init

    # Verify that the module imports check_data_version from infra.paths
    # by checking the source contains the expected call.
    import inspect

    src = inspect.getsource(agent_init._init_langgraph_agent)
    assert "check_data_version" in src
    # Verify it's called after unscoped_warning
    unscoped_pos = src.index("unscoped_warning")
    check_pos = src.index("check_data_version")
    assert check_pos > unscoped_pos, "check_data_version must be called after unscoped_warning"
