"""Pip-less wheel installer for frozen-app plugin deps (ADR 0093 P1)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from graph.plugins import wheel_installer as wi


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A clean instance root so the deps dir + lock land under tmp, never the real repo."""
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(tmp_path / "home" / "plugins"))
    from infra.paths import reset_instance_paths

    reset_instance_paths()
    return tmp_path


# ── pure-wheel classification ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "pure"),
    [
        ("rich-13.7.0-py3-none-any.whl", True),
        ("six-1.16.0-py2.py3-none-any.whl", True),
        ("lxml-5.1.0-cp312-cp312-manylinux_2_28_x86_64.whl", False),  # platform wheel
        ("numpy-1.26-cp312-cp312-macosx_11_0_arm64.whl", False),
        ("pkg-1.0.tar.gz", False),  # sdist
        ("weird", False),
    ],
)
def test_is_pure_wheel(filename, pure):
    assert wi._is_pure_wheel(filename) is pure


# ── version + wheel selection ────────────────────────────────────────────────


def _pypi_index(name, releases):
    """Shape a PyPI /json 'releases' map: {version: [file-records]}."""
    return {"info": {"requires_dist": []}, "releases": releases}


def _wheel_file(name, version, *, pure=True, sha="0" * 64):
    tag = "py3-none-any" if pure else "cp312-cp312-macosx_11_0_arm64"
    fn = f"{name}-{version}-{tag}.whl"
    return {"filename": fn, "packagetype": "bdist_wheel", "url": f"https://f/{fn}", "digests": {"sha256": sha}}


def test_select_picks_highest_matching_pure_wheel(monkeypatch):
    releases = {
        "1.0.0": [_wheel_file("rich", "1.0.0")],
        "2.0.0": [_wheel_file("rich", "2.0.0")],
        "2.1.0b1": [_wheel_file("rich", "2.1.0b1")],  # prerelease — skipped
        "3.0.0": [_wheel_file("rich", "3.0.0")],
    }
    monkeypatch.setattr(wi, "_pypi_json", lambda name, version=None: _pypi_index(name, releases))
    version, wheel = wi._select("rich", ">=2,<3")
    assert version == "2.0.0"  # highest in [2,3), prerelease excluded


def test_select_refuses_when_only_platform_wheels(monkeypatch):
    releases = {"5.1.0": [_wheel_file("lxml", "5.1.0", pure=False)]}
    monkeypatch.setattr(wi, "_pypi_json", lambda name, version=None: _pypi_index(name, releases))
    with pytest.raises(wi.WheelInstallError, match="pure-Python wheel is required"):
        wi._select("lxml", "")


def test_select_refuses_when_no_version_matches(monkeypatch):
    releases = {"1.0.0": [_wheel_file("rich", "1.0.0")]}
    monkeypatch.setattr(wi, "_pypi_json", lambda name, version=None: _pypi_index(name, releases))
    with pytest.raises(wi.WheelInstallError, match="no version matches"):
        wi._select("rich", ">=9")


# ── transitive resolution: markers, specifiers, cycles, short-circuit ─────────


def test_resolve_short_circuits_already_satisfied(monkeypatch):
    called = []
    monkeypatch.setattr(wi, "_select", lambda n, s: called.append(n) or ("1.0.0", _wheel_file(n, "1.0.0")))
    monkeypatch.setattr(wi, "_deps_of", lambda n, v: [])
    plan = wi.resolve(["rich>=1"], already_satisfied=lambda n: n == "rich")
    assert plan == [] and called == []  # bundled → never hits PyPI


def test_resolve_follows_transitive_and_dedupes(monkeypatch):
    graph = {"top": ["mid (>=1)"], "mid": ["leaf"], "leaf": []}
    monkeypatch.setattr(wi, "_select", lambda n, s: ("1.0.0", _wheel_file(n, "1.0.0")))
    monkeypatch.setattr(wi, "_deps_of", lambda n, v: graph.get(n, []))
    names = [n for n, _v, _w in wi.resolve(["top"], already_satisfied=lambda n: False)]
    assert set(names) == {"top", "mid", "leaf"}


def test_resolve_drops_marker_gated_and_extra_deps(monkeypatch):
    # requires_dist with a false marker (extra) + a platform marker that won't match.
    graph = {"top": ['colorama ; extra == "windows"', 'pywin32 ; sys_platform == "win32"']}
    monkeypatch.setattr(wi, "_select", lambda n, s: ("1.0.0", _wheel_file(n, "1.0.0")))
    monkeypatch.setattr(wi, "_deps_of", lambda n, v: graph.get(n, []))
    names = [n for n, _v, _w in wi.resolve(["top"], already_satisfied=lambda n: False)]
    assert names == ["top"]  # extra-gated dropped; win32 marker false on this host (CI is linux/mac)


def test_resolve_guards_cycles(monkeypatch):
    graph = {"a": ["b"], "b": ["a"]}  # a↔b cycle
    monkeypatch.setattr(wi, "_select", lambda n, s: ("1.0.0", _wheel_file(n, "1.0.0")))
    monkeypatch.setattr(wi, "_deps_of", lambda n, v: graph.get(n, []))
    names = [n for n, _v, _w in wi.resolve(["a"], already_satisfied=lambda n: False)]
    assert set(names) == {"a", "b"}  # visited-set stops the cycle, doesn't loop forever


# ── download + verify + unpack + lock ─────────────────────────────────────────


def _make_wheel(pkg="demolib", version="1.0.0", module="demolib") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{module}/__init__.py", "VALUE = 42\n")
        zf.writestr(f"{pkg}-{version}.dist-info/METADATA", f"Name: {pkg}\nVersion: {version}\n")
    return buf.getvalue()


def test_download_verified_rejects_hash_mismatch(monkeypatch):
    data = _make_wheel()
    monkeypatch.setattr(wi, "_http_get", None, raising=False)
    import graph.plugins.installer as inst

    monkeypatch.setattr(inst, "_http_get", lambda url, **k: type("R", (), {"content": data})())
    wheel = {"filename": "demolib-1.0.0-py3-none-any.whl", "url": "https://f/x.whl", "digests": {"sha256": "0" * 64}}
    with pytest.raises(wi.WheelInstallError, match="!= pinned"):
        wi._download_verified(wheel)


def test_install_end_to_end_unpacks_pins_and_syspaths(box, monkeypatch):
    data = _make_wheel(module="demolib")
    sha = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(wi, "_select", lambda n, s: ("1.0.0", {"filename": f"{n}-1.0.0-py3-none-any.whl", "url": "https://f/w.whl", "digests": {"sha256": sha}}))
    monkeypatch.setattr(wi, "_deps_of", lambda n, v: [])
    import graph.plugins.installer as inst

    monkeypatch.setattr(inst, "lock_path", lambda: box / "plugins.lock")
    monkeypatch.setattr(inst, "_http_get", lambda url, **k: type("R", (), {"content": data})())

    installed = wi.install("demoplug", ["demolib>=1"], already_satisfied=lambda n: False)
    assert installed == ["demolib==1.0.0"]
    # unpacked into the per-plugin deps dir…
    dest = wi.plugin_deps_dir("demoplug")
    assert (dest / "demolib" / "__init__.py").exists()
    # …the dir is on sys.path (hot-adopt, no restart)…
    import sys

    assert str(dest) in sys.path
    # …and the resolved dep is pinned in the lock.
    lock = json.loads((box / "plugins.lock").read_text())
    assert lock["demoplug"]["deps"] == [{"name": "demolib", "version": "1.0.0", "sha256": sha}]


def test_unpack_wheel_rejects_traversal(box):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.py", "nope")
    with pytest.raises(wi.WheelInstallError, match="unsafe path"):
        wi._unpack_wheel(buf.getvalue(), wi.plugin_deps_dir("x"))
