"""Managed Python runtime: path resolution + provisioning (ADR 0094)."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from infra import python_runtime as pr
from infra.paths import reset_instance_paths
from runtime import python_install as pi


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A clean box root: PROTOAGENT_BOX_ROOT under tmp, so the host's real managed
    runtime (if any) never leaks into these assertions."""
    box_root = tmp_path / "box"
    box_root.mkdir()
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(box_root))
    reset_instance_paths()
    return box_root


def _make_python(install_dir: Path, *, version: str = pi.PYTHON_VERSION) -> Path:
    """Drop a runnable fake interpreter at the layout install_only extracts to."""
    exe = pr._python_exe_in(install_dir)
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f"#!/bin/sh\necho Python {version}\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


@pytest.fixture
def req_file(tmp_path, monkeypatch):
    """A stand-in requirements-docs.txt the baseline machinery resolves to."""
    req = tmp_path / "requirements-docs.txt"
    req.write_text("python-docx>=1.1\nopenpyxl>=3.1\n", encoding="utf-8")
    monkeypatch.setattr(pi, "_baseline_requirements_path", lambda: req)
    return req


# ── path resolution ──────────────────────────────────────────────────────────


def test_managed_root_is_box_scoped(box):
    assert pr.managed_python_root() == box / "runtime" / "python"
    assert pr.managed_python_install_dir() == box / "runtime" / "python" / "current"


def test_exe_none_until_interpreter_present(box):
    assert pr.managed_python_exe() is None
    exe = _make_python(pr.managed_python_install_dir())
    assert pr.managed_python_exe() == exe


# ── platform/arch mapping ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sysplat", "machine", "expected"),
    [
        ("darwin", "arm64", ("darwin", "arm64")),
        ("darwin", "x86_64", ("darwin", "x64")),
        ("linux", "aarch64", ("linux", "arm64")),
        ("linux", "x86_64", ("linux", "x64")),
        ("win32", "amd64", ("win", "x64")),
    ],
)
def test_platform_arch_mapping(monkeypatch, sysplat, machine, expected):
    monkeypatch.setattr(pi.sys, "platform", sysplat)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert pi._platform_arch() == expected


def test_platform_arch_rejects_unknown(monkeypatch):
    monkeypatch.setattr(pi.sys, "platform", "sunos5")
    monkeypatch.setattr("platform.machine", lambda: "sparc")
    with pytest.raises(pi.UnsupportedPlatform):
        pi._platform_arch()
    # a supported platform on an unpinned arch is still rejected (no win-arm64 pin)
    monkeypatch.setattr(pi.sys, "platform", "win32")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    with pytest.raises(pi.UnsupportedPlatform):
        pi._platform_arch()


def test_archive_name_and_url():
    assert pi._archive_name("darwin", "arm64") == (
        f"cpython-{pi.PYTHON_VERSION}+{pi.PBS_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
    )
    assert pi._dist_url("linux", "x64") == (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{pi.PBS_RELEASE}/cpython-{pi.PYTHON_VERSION}+{pi.PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz"
    )


def test_pin_tables_are_self_consistent():
    # The install path indexes _SHA256 and _TRIPLE by the same resolved (plat, arch);
    # a missing/misshapen entry would only surface at install time on that host.
    assert set(pi._SHA256) == set(pi._TRIPLE)
    for digest in pi._SHA256.values():
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


# ── status ───────────────────────────────────────────────────────────────────


def test_status_reports_managed_and_baseline(box, req_file):
    st = pi.python_status()
    assert st["managed"] is False and st["needed"] is False  # tests never run frozen

    install_dir = pr.managed_python_install_dir()
    _make_python(install_dir)
    (install_dir / pi._VERSION_MARKER).write_text(pi.PYTHON_VERSION, encoding="utf-8")

    st = pi.python_status()
    assert st["managed"] is True
    assert st["managed_version"] == pi.PYTHON_VERSION
    assert st["target_version"] == pi.PYTHON_VERSION
    assert st["baseline_installed"] is False and st["baseline_current"] is False

    # Stamp the baseline at the current requirements hash → installed AND current…
    (install_dir / pi._BASELINE_MARKER).write_text(pi._baseline_hash(req_file), encoding="utf-8")
    st = pi.python_status()
    assert st["baseline_installed"] is True and st["baseline_current"] is True

    # …then change the pins: still installed, no longer current (repair territory).
    req_file.write_text("python-docx>=2.0\n", encoding="utf-8")
    st = pi.python_status()
    assert st["baseline_installed"] is True and st["baseline_current"] is False


# ── provisioning (download mocked with a local fixture archive) ──────────────


def _fake_targz() -> bytes:
    """A minimal install_only-shaped ``.tar.gz``: one top-level ``python/`` dir with
    the interpreter at the expected path."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = b"#!/bin/sh\necho Python TEST\n"
        rel = "python/python.exe" if os.name == "nt" else "python/bin/python3"
        info = tarfile.TarInfo(rel)
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.fixture
def mocked_install(box, req_file, monkeypatch):
    """Serve a locally-built archive whose real hash is pinned, and stub the pip phase
    (recording calls + stamping the marker like the real one), so install() runs its
    full verify+extract+swap+deps flow with no network and no real interpreter."""
    plat, arch = pi._platform_arch()
    archive = _fake_targz()
    monkeypatch.setitem(pi._SHA256, (plat, arch), hashlib.sha256(archive).hexdigest())

    downloads = {"n": 0}

    def _fake_download(url, dest, on_progress, timeout):  # noqa: ANN001, ARG001
        downloads["n"] += 1
        Path(dest).write_bytes(archive)
        if on_progress:
            on_progress(len(archive), len(archive))

    baseline = {"n": 0}

    def _fake_baseline(exe):  # noqa: ANN001, ARG001
        baseline["n"] += 1
        (pr.managed_python_install_dir() / pi._BASELINE_MARKER).write_text(
            pi._baseline_hash(req_file), encoding="utf-8"
        )
        return True

    monkeypatch.setattr(pi, "_download_archive", _fake_download)
    monkeypatch.setattr(pi, "_install_baseline", _fake_baseline)
    return {"downloads": downloads, "baseline": baseline, "req": req_file}


def test_install_provisions_interpreter_and_baseline(mocked_install):
    st = pi.install_managed_python()
    assert pr.managed_python_exe() is not None
    assert st["managed"] is True and st["managed_version"] == pi.PYTHON_VERSION
    assert st["baseline_installed"] is True and st["baseline_current"] is True
    assert mocked_install["downloads"]["n"] == 1
    assert mocked_install["baseline"]["n"] == 1
    # marker written pre-swap
    marker = pr.managed_python_install_dir() / pi._VERSION_MARKER
    assert marker.read_text().strip() == pi.PYTHON_VERSION


def test_install_idempotent_then_force(mocked_install):
    pi.install_managed_python()
    # matching interpreter + current baseline → full short-circuit
    pi.install_managed_python()
    assert mocked_install["downloads"]["n"] == 1
    assert mocked_install["baseline"]["n"] == 1
    # force reinstalls both phases
    pi.install_managed_python(force=True)
    assert mocked_install["downloads"]["n"] == 2
    assert mocked_install["baseline"]["n"] == 2


def test_stale_baseline_repairs_without_redownload(mocked_install):
    pi.install_managed_python()
    # A pin bump: the interpreter still matches, the baseline no longer does.
    mocked_install["req"].write_text("python-docx>=2.0\n", encoding="utf-8")
    st = pi.install_managed_python()
    assert mocked_install["downloads"]["n"] == 1  # no re-download
    assert mocked_install["baseline"]["n"] == 2  # deps-only repair ran
    assert st["baseline_current"] is True


def test_install_rejects_bad_hash(box, req_file, monkeypatch):
    plat, arch = pi._platform_arch()
    archive = _fake_targz()
    monkeypatch.setitem(pi._SHA256, (plat, arch), "0" * 64)  # wrong digest

    def _fake(url, dest, on_progress, timeout):  # noqa: ANN001, ARG001
        Path(dest).write_bytes(archive)

    monkeypatch.setattr(pi, "_download_archive", _fake)
    with pytest.raises(pi.PythonInstallError, match="integrity check failed"):
        pi.install_managed_python()
    assert pr.managed_python_exe() is None  # nothing left behind


def test_extract_rejects_unsafe_members(box, req_file, monkeypatch):
    plat, arch = pi._platform_arch()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"nope"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    archive = buf.getvalue()
    monkeypatch.setitem(pi._SHA256, (plat, arch), hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(pi, "_download_archive", lambda url, dest, p, t: Path(dest).write_bytes(archive))
    with pytest.raises(pi.PythonInstallError, match="unsafe archive member"):
        pi.install_managed_python()


# ── managed runtime distributions + per-plugin dep install (ADR 0094 P2) ──────


def test_managed_runtime_distributions_reads_and_normalizes(box):
    site = pr.managed_python_install_dir() / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "python_docx-1.1.0.dist-info").mkdir()
    (site / "openpyxl-3.1.5.dist-info").mkdir()
    (site / "Some.Weird_Name-2.0.egg-info").mkdir()
    dists = pr.managed_runtime_distributions()
    assert "python-docx" in dists  # dist-info uses python_docx → normalized to python-docx
    assert "openpyxl" in dists
    assert "some-weird-name" in dists  # egg-info + PEP 503 normalization


def test_managed_runtime_distributions_empty_when_unprovisioned(box):
    assert pr.managed_runtime_distributions() == set()


# ── pytest_interpreter: the self-building loop's frozen path (ADR 0096 D3) ────


def _install_pytest_dist(version: str = "8.3.2") -> None:
    """Make ``pytest`` visible to ``managed_runtime_distributions()``."""
    site = pr.managed_python_install_dir() / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / f"pytest-{version}.dist-info").mkdir()


def test_pytest_interpreter_source_run_uses_sys_executable(box, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    exe, err = pr.pytest_interpreter()
    assert err is None
    assert exe == sys.executable


def test_pytest_interpreter_frozen_refuses_without_runtime(box, monkeypatch):
    """No managed runtime at all — refuse, and name the provisioning step."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    exe, err = pr.pytest_interpreter()
    assert exe is None
    assert "Settings" in err and "runtime" in err


def test_pytest_interpreter_frozen_refuses_when_runtime_lacks_pytest(box, monkeypatch):
    """The regression this ADR 0096 D3 gap actually was: a runtime is provisioned
    (so ``managed_python_exe()`` is happy) but its baseline is the ADR 0092 document
    stack, which carries no pytest. Spawning would die on ``No module named pytest``;
    the refusal has to name the fix instead."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _make_python(pr.managed_python_install_dir())
    site = pr.managed_python_install_dir() / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "python_docx-1.1.0.dist-info").mkdir()  # the document baseline, no pytest
    exe, err = pr.pytest_interpreter()
    assert exe is None
    assert "pytest" in err and "Install deps" in err


def test_pytest_interpreter_frozen_returns_managed_python_when_pytest_present(box, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    expected = _make_python(pr.managed_python_install_dir())
    _install_pytest_dist()
    exe, err = pr.pytest_interpreter()
    assert err is None
    assert exe == str(expected)


def test_pytest_interpreter_never_falls_back_to_a_system_python(box, monkeypatch):
    """The module's standing rule (see its docstring): a discovered system Python of
    arbitrary version reproduces #2137's silently-missing-capability class. A frozen
    build with no managed runtime must refuse, never hand back ``sys.executable`` —
    which in a PyInstaller bundle is the server binary and would relaunch the app."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    exe, _ = pr.pytest_interpreter()
    assert exe is None
    assert exe != sys.executable


def test_install_requirements_refuses_when_unprovisioned(box):
    with pytest.raises(pi.PythonInstallError, match="isn't provisioned"):
        pi.install_requirements_into_managed_runtime(["python-docx>=1.1"])


def test_install_requirements_pips_into_provisioned_runtime(box, monkeypatch):
    _make_python(pr.managed_python_install_dir())
    calls = []
    monkeypatch.setattr(pi, "_pip_install", lambda exe, spec, *, what, timeout=600.0: calls.append(spec))
    st = pi.install_requirements_into_managed_runtime(["python-docx>=1.1", "  ", ""])
    assert calls == [["--", "python-docx>=1.1"]]  # blanks dropped, bare specs (not -r)
    assert st["managed"] is True


def test_test_runtime_missing_reports_all_when_unprovisioned(box):
    assert pi.test_runtime_missing() == pi.TEST_RUNTIME_REQUIREMENTS


def test_test_runtime_missing_ignores_host_importability(box):
    """The #2638 trap: every one of these IS importable in this process, and that must
    not count — the child has separate site-packages."""
    _make_python(pr.managed_python_install_dir())
    site = pr.managed_python_install_dir() / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "pytest-9.1.1.dist-info").mkdir()
    missing = pi.test_runtime_missing()
    assert "pytest>=8" not in missing  # the child has it
    assert "langchain-core>=0.2" in missing  # importable HERE, absent THERE


def test_ensure_test_runtime_installs_only_what_is_missing(box, monkeypatch):
    _make_python(pr.managed_python_install_dir())
    site = pr.managed_python_install_dir() / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for d in ("pytest-9.1.1", "pyyaml-6.0.2", "langchain_core-0.3.0", "fastapi-0.115.0"):
        (site / f"{d}.dist-info").mkdir()
    calls = []
    monkeypatch.setattr(pi, "_pip_install", lambda exe, spec, *, what, timeout=600.0: calls.append(spec))
    assert pi.ensure_test_runtime() is None
    assert calls == [["--", "httpx>=0.27"]]  # only the one gap, not all five


def test_ensure_test_runtime_is_a_noop_once_satisfied(box, monkeypatch):
    _make_python(pr.managed_python_install_dir())
    site = pr.managed_python_install_dir() / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    # Versions that actually SATISFY each specifier — seeding "1.0" for everything would
    # fail the version check, which is the point of it.
    for name, version in (
        ("pytest", "9.1.1"),
        ("pyyaml", "6.0.2"),
        ("langchain_core", "0.3.0"),
        ("fastapi", "0.115.0"),
        ("httpx", "0.28.1"),
    ):
        (site / f"{name}-{version}.dist-info").mkdir()
    monkeypatch.setattr(pi, "_pip_install", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no pip")))
    assert pi.ensure_test_runtime() is None  # second call costs a dist-info scan, nothing more


def test_ensure_test_runtime_names_provisioning_when_absent(box):
    err = pi.ensure_test_runtime()
    assert err and "provisioned" in err and "Settings" in err


def _seed_dists(**name_to_version: str) -> None:
    site = pr.managed_python_install_dir() / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for name, version in name_to_version.items():
        (site / f"{name}-{version}.dist-info").mkdir(exist_ok=True)


def test_a_dist_below_its_minimum_is_missing_not_satisfied(box):
    """Name-presence is the wrong question: a plugin's own requires_pip pins into this
    same runtime, so an old langchain-core can sit here satisfying the NAME while
    failing the specifier the scaffolded suite needs."""
    _make_python(pr.managed_python_install_dir())
    _seed_dists(pytest="1.0", pyyaml="6.0.2", langchain_core="0.1.9", fastapi="0.115.0", httpx="0.28.1")
    missing = pi.test_runtime_missing()
    assert "pytest>=8" in missing  # 1.0 does not satisfy >=8
    assert "langchain-core>=0.2" in missing  # 0.1.9 does not satisfy >=0.2
    assert "pyyaml>=6" not in missing and "httpx>=0.27" not in missing


def test_a_dist_below_minimum_reaches_the_pip_arguments(box, monkeypatch):
    _make_python(pr.managed_python_install_dir())
    _seed_dists(pytest="1.0", pyyaml="6.0.2", langchain_core="0.3.0", fastapi="0.115.0", httpx="0.28.1")
    calls = []
    monkeypatch.setattr(pi, "_pip_install", lambda exe, spec, *, what, timeout=600.0: calls.append(spec))
    assert pi.ensure_test_runtime() is None
    assert calls == [["--", "pytest>=8"]]  # the stale one, and only it


def test_versions_are_read_off_the_dist_info_names(box):
    _make_python(pr.managed_python_install_dir())
    _seed_dists(pytest="9.1.1", langchain_core="0.3.0")
    have = pr.managed_runtime_distribution_versions()
    assert have["pytest"] == "9.1.1"
    assert have["langchain-core"] == "0.3.0"  # dist-info underscore → PEP 503 dash


def test_an_unparseable_installed_version_is_reinstalled_not_trusted(box):
    """Safe direction: pip is idempotent, a wrong 'satisfied' is a broken child."""
    _make_python(pr.managed_python_install_dir())
    _seed_dists(pytest="not-a-version")
    assert "pytest>=8" in pi.test_runtime_missing()


def test_install_requirements_empty_is_noop(box, monkeypatch):
    _make_python(pr.managed_python_install_dir())
    monkeypatch.setattr(pi, "_pip_install", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not pip")))
    st = pi.install_requirements_into_managed_runtime([])
    assert st["managed"] is True  # nothing to install → no pip, still returns status
