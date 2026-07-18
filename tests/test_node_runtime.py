"""Managed Node runtime: path resolution, PATH augmentation, and provisioning (ADR 0085)."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from infra import node_runtime as nr
from infra.paths import reset_instance_paths
from runtime import node_install as ni


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A clean, node-free box root: PROTOAGENT_BOX_ROOT under tmp + a PATH with no node,
    so the host's real Node install never leaks into these assertions."""
    box_root = tmp_path / "box"
    box_root.mkdir()
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(box_root))
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    reset_instance_paths()
    return box_root


def _make_node(bin_dir: Path, *, version: str = "v24.18.0") -> Path:
    """Drop a runnable fake ``node`` (POSIX) / ``node.exe`` (Windows) into ``bin_dir``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / nr._node_exe_name()
    exe.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


# ── path resolution ──────────────────────────────────────────────────────────


def test_managed_root_is_box_scoped(box):
    assert nr.managed_node_root() == box / "runtime" / "node"
    assert nr.managed_node_install_dir() == box / "runtime" / "node" / "current"


def test_bin_dir_none_until_node_present(box):
    assert nr.managed_node_bin_dir() is None
    _make_node(nr._bin_dir_for(nr.managed_node_install_dir()))
    assert nr.managed_node_bin_dir() == nr._bin_dir_for(nr.managed_node_install_dir())


# ── PATH augmentation (the consumption seam) ─────────────────────────────────


def test_augment_appends_managed_when_no_node(box):
    bin_dir = nr._bin_dir_for(nr.managed_node_install_dir())
    _make_node(bin_dir)

    added = nr.augment_path_with_managed_node()
    assert added == str(bin_dir)
    assert str(bin_dir) in os.environ["PATH"].split(os.pathsep)
    # idempotent — already present, so a second call is a no-op
    assert nr.augment_path_with_managed_node() is None


def test_augment_noop_when_user_node_present(box, tmp_path, monkeypatch):
    user_bin = tmp_path / "userbin"
    _make_node(user_bin)
    monkeypatch.setenv("PATH", str(user_bin))
    # a managed install also exists, but the user's own node wins → we don't touch PATH
    _make_node(nr._bin_dir_for(nr.managed_node_install_dir()))

    before = os.environ["PATH"]
    assert nr.augment_path_with_managed_node() is None
    assert os.environ["PATH"] == before


def test_augment_noop_when_nothing_provisioned(box):
    assert nr.augment_path_with_managed_node() is None


# ── platform/arch mapping ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sysplat", "machine", "expected"),
    [
        ("darwin", "arm64", ("darwin", "arm64")),
        ("darwin", "x86_64", ("darwin", "x64")),
        ("linux", "aarch64", ("linux", "arm64")),
        ("linux", "x86_64", ("linux", "x64")),
    ],
)
def test_platform_arch_mapping(monkeypatch, sysplat, machine, expected):
    monkeypatch.setattr(ni.sys, "platform", sysplat)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert ni._platform_arch() == expected


def test_platform_arch_rejects_unknown(monkeypatch):
    monkeypatch.setattr(ni.sys, "platform", "sunos5")
    monkeypatch.setattr("platform.machine", lambda: "sparc")
    with pytest.raises(ni.UnsupportedPlatform):
        ni._platform_arch()
    # a supported platform on an unsupported arch is still rejected
    monkeypatch.setattr(ni.sys, "platform", "linux")
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    with pytest.raises(ni.UnsupportedPlatform):
        ni._platform_arch()


def test_archive_name_and_url():
    assert ni._archive_name("darwin", "arm64") == f"node-{ni.NODE_VERSION}-darwin-arm64.tar.gz"
    assert ni._archive_name("win", "x64") == f"node-{ni.NODE_VERSION}-win-x64.zip"
    assert ni._dist_url("linux", "x64") == (
        f"https://nodejs.org/dist/{ni.NODE_VERSION}/node-{ni.NODE_VERSION}-linux-x64.tar.gz"
    )


def test_every_pinned_arch_has_a_hash():
    # The install path indexes _SHA256 by the resolved (plat, arch); a missing entry
    # would only surface at install time on that host. Assert the table is self-consistent.
    for key in ni._SHA256:
        assert len(ni._SHA256[key]) == 64
        assert all(c in "0123456789abcdef" for c in ni._SHA256[key])


# ── status ───────────────────────────────────────────────────────────────────


def test_status_reports_managed(box):
    assert nr.node_on_path() is None  # box fixture guarantees a node-free PATH
    st = ni.node_status()
    assert st["source"] is None and not st["managed"]

    install_dir = nr.managed_node_install_dir()
    _make_node(nr._bin_dir_for(install_dir))
    (install_dir / ni._VERSION_MARKER).write_text(ni.NODE_VERSION, encoding="utf-8")

    st = ni.node_status()
    assert st["managed"] is True
    assert st["source"] == "managed"
    assert st["managed_version"] == ni.NODE_VERSION
    assert st["target_version"] == ni.NODE_VERSION


# ── provisioning (download mocked with a local fixture archive) ──────────────


def _fake_targz() -> bytes:
    """A minimal Node-shaped ``.tar.gz``: one top-level ``node-*`` dir with ``bin/node``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = b"#!/bin/sh\necho vTEST\n"
        info = tarfile.TarInfo(f"node-{ni.NODE_VERSION}-test/bin/{nr._node_exe_name()}")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.fixture
def mocked_download(box, monkeypatch):
    """Serve a locally-built archive whose real hash is pinned, so install() runs its
    full verify+extract+swap path with no network."""
    if sys.platform.startswith("win"):
        pytest.skip("fixture archive is a tarball; Windows uses the zip path")
    plat, arch = ni._platform_arch()
    archive = _fake_targz()
    monkeypatch.setitem(ni._SHA256, (plat, arch), hashlib.sha256(archive).hexdigest())

    def _fake(url, dest, on_progress, timeout):  # noqa: ANN001, ARG001
        Path(dest).write_bytes(archive)
        if on_progress:
            on_progress(len(archive), len(archive))

    monkeypatch.setattr(ni, "_download_archive", _fake)
    return archive


def test_install_provisions_and_augments(mocked_download):
    st = ni.install_managed_node()
    bin_dir = nr._bin_dir_for(nr.managed_node_install_dir())
    assert (bin_dir / nr._node_exe_name()).exists()
    assert st["managed"] is True
    assert st["managed_version"] == ni.NODE_VERSION
    # install re-augments PATH so a live server hot-adopts it
    assert str(bin_dir) in os.environ["PATH"].split(os.pathsep)
    # marker written pre-swap
    assert (nr.managed_node_install_dir() / ni._VERSION_MARKER).read_text().strip() == ni.NODE_VERSION


def test_install_idempotent_then_force(mocked_download, monkeypatch):
    ni.install_managed_node()
    calls = {"n": 0}
    orig = ni._download_archive

    def _counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(ni, "_download_archive", _counting)
    # matching version already installed → short-circuit, no re-download
    ni.install_managed_node()
    assert calls["n"] == 0
    # force reinstalls
    ni.install_managed_node(force=True)
    assert calls["n"] == 1


def test_install_rejects_bad_hash(box, monkeypatch):
    if sys.platform.startswith("win"):
        pytest.skip("tarball fixture")
    plat, arch = ni._platform_arch()
    archive = _fake_targz()
    monkeypatch.setitem(ni._SHA256, (plat, arch), "0" * 64)  # wrong digest

    def _fake(url, dest, on_progress, timeout):  # noqa: ANN001, ARG001
        Path(dest).write_bytes(archive)

    monkeypatch.setattr(ni, "_download_archive", _fake)
    with pytest.raises(ni.NodeInstallError, match="integrity check failed"):
        ni.install_managed_node()
    assert nr.managed_node_bin_dir() is None  # nothing left behind
