"""macOS freezes the desktop server as a signed ONEDIR tree, not a onefile.

The v0.231.0 stress test (2026-11-07) timed the desktop CLI at 14-16 s per call, ``--help``
included, for ~1.3 s of CPU. A PyInstaller onefile unpacks ~5,000 files into a fresh
``$TMPDIR/_MEI*`` dir on every launch, and macOS scans every freshly written Mach-O on first
load (``sample``: 2,360 of 2,567 main-thread samples in dyld's signature registration). A
onedir tree at one stable path inside the app pays that once. These pin the layout and the
signing that layout needs, since Tauri signs only the binaries it bundles itself.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = ROOT / "apps" / "desktop" / "src-tauri"


@pytest.fixture(scope="module")
def bs():
    spec = importlib.util.spec_from_file_location(
        "build_sidecar", ROOT / "apps" / "desktop" / "sidecar" / "build_sidecar.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_macos_freezes_onedir_other_platforms_onefile(bs):
    assert bs.freeze_mode("darwin") == "onedir"
    assert bs.freeze_mode("linux") == "onefile"
    assert bs.freeze_mode("win32") == "onefile"
    src = (ROOT / "apps" / "desktop" / "sidecar" / "build_sidecar.py").read_text()
    assert '"--onefile",' not in src, "the PyInstaller mode must come from freeze_mode()"


def test_macos_bundles_the_onedir_tree_in_resources(bs):
    """Contents/MacOS would make the bootloader look in Contents/Frameworks (where codesign
    refuses data files), so the tree goes to Resources and replaces the externalBin."""
    conf = json.loads((SRC_TAURI / "tauri.macos.conf.json").read_text())
    assert conf["bundle"]["externalBin"] == []
    rel = bs.ONEDIR_DEST.relative_to(SRC_TAURI).as_posix() + "/"
    assert conf["bundle"]["resources"] == {rel: "protoagent-server/"}
    # sidecar.rs launches it from exactly there.
    rs = (SRC_TAURI / "src" / "sidecar.rs").read_text()
    assert 'resource_dir.join("protoagent-server").join("protoagent-server")' in rs
    assert '.sidecar("protoagent-server")' in rs  # still the Linux/Windows path


def _macho(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 60)
    return path


def test_is_macho_reads_magic_not_suffix(bs, tmp_path):
    assert bs.is_macho(_macho(tmp_path / "Python"))
    assert bs.is_macho(_macho(tmp_path / "x.cpython-312-darwin.so"))
    fat = tmp_path / "fat"
    fat.write_bytes(b"\xca\xfe\xba\xbe\0\0\0\x02" + b"\0" * 40)
    assert bs.is_macho(fat)
    java = tmp_path / "A.class"
    java.write_bytes(b"\xca\xfe\xba\xbe\0\0\0\x41" + b"\0" * 40)  # class file v65
    assert not bs.is_macho(java)
    (tmp_path / "data.json").write_text("{}")
    assert not bs.is_macho(tmp_path / "data.json")


_UNIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the onedir freeze is macOS-only; symlinks need privileges on Windows"
)


def _python_framework(internal: Path) -> tuple[Path, Path]:
    """PyInstaller's layout for a python.org (framework) Python, as the CI runner has."""
    fw = internal / "Python.framework"
    lib = _macho(fw / "Versions" / "3.12" / "Python")
    (fw / "Versions" / "3.12" / "Resources").mkdir()
    (fw / "Versions" / "3.12" / "Resources" / "Info.plist").write_text("plist")
    os.symlink("3.12", fw / "Versions" / "Current")
    os.symlink("Versions/Current/Python", fw / "Python")
    os.symlink("Versions/Current/Resources", fw / "Resources")
    os.symlink("Python.framework/Versions/3.12/Python", internal / "Python")
    return fw, lib


@pytest.mark.platform_sensitive
@_UNIX_ONLY
def test_materialize_symlinks_leaves_no_links_and_keeps_content(bs, tmp_path):
    """Tauri skips directory symlinks when it copies a resource tree, so a linked
    directory would vanish; the tree must hold real files before it's signed."""
    root = tmp_path / "onedir"
    lib = _macho(root / "_internal" / "pkg" / "real" / "libx.dylib")
    os.symlink("real", root / "_internal" / "pkg" / "current")
    os.symlink("pkg/real/libx.dylib", root / "_internal" / "libx.dylib")
    os.symlink("nowhere", root / "_internal" / "dangling")

    assert bs.materialize_symlinks(root) == 3
    assert not [p for p in root.rglob("*") if p.is_symlink()]
    assert (root / "_internal" / "libx.dylib").read_bytes() == lib.read_bytes()
    assert (root / "_internal" / "pkg" / "current" / "libx.dylib").read_bytes() == lib.read_bytes()
    assert not (root / "_internal" / "dangling").exists()


@pytest.mark.platform_sensitive
@_UNIX_ONLY
def test_a_framework_is_dissolved_into_its_bare_binary(bs, tmp_path):
    """Desktop-build run 42455809696: materializing Python.framework left a real
    ``Python.framework/Python`` at the bundle root, and codesign refused it ("bundle format
    is ambiguous"). The binary moves to ``_internal/Python``, where the bootloader loads it,
    and the bundle goes away."""
    internal = tmp_path / "onedir" / "_internal"
    _fw, lib = _python_framework(internal)
    content = lib.read_bytes()

    assert bs.dissolve_frameworks(tmp_path / "onedir") == ["Python"]
    assert not (internal / "Python.framework").exists()
    assert not (internal / "Python").is_symlink()
    assert (internal / "Python").read_bytes() == content
    assert bs.materialize_symlinks(tmp_path / "onedir") == 0


@pytest.mark.platform_sensitive
@_UNIX_ONLY
def test_a_framework_with_more_than_its_binary_is_refused_not_dropped(bs, tmp_path):
    internal = tmp_path / "onedir" / "_internal"
    fw, _lib = _python_framework(internal)
    (fw / "Versions" / "3.12" / "Resources" / "icon.icns").write_text("x")
    with pytest.raises(SystemExit, match="more than its binary"):
        bs.dissolve_frameworks(tmp_path / "onedir")
    assert fw.is_dir()


def test_load_commands_into_a_dissolved_framework_are_rewritten_beside_it(bs):
    names = ["Python"]
    assert bs.dissolved_ref("@rpath/Python.framework/Versions/3.12/Python", names) == "@rpath/Python"
    assert bs.dissolved_ref("@rpath/Python.framework/Versions/Current/Python", names) == "@rpath/Python"
    assert bs.dissolved_ref("@loader_path/../Python.framework/Python", names) == "@loader_path/../Python"
    # System frameworks and everything else stay as they are.
    assert bs.dissolved_ref("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", names) is None
    assert bs.dissolved_ref("/Library/Frameworks/Python.framework/Versions/3.12/Python", names) is None
    assert bs.dissolved_ref("@rpath/libssl.3.dylib", names) is None
    assert bs.dissolved_ref("@rpath/Tcl.framework/Versions/8.6/Tcl", names) is None


def test_verify_script_rejects_a_framework_left_in_the_tree():
    sh = (ROOT / "scripts" / "verify-macos-desktop.sh").read_text()
    assert "-name '*.framework'" in sh


def test_codesign_plan_signs_every_macho_inside_out_and_the_bootloader_last(bs, tmp_path):
    root = tmp_path / "onedir"
    exe = _macho(root / "protoagent-server")
    deep = _macho(root / "_internal" / "PIL" / ".dylibs" / "libz.dylib")
    ext = _macho(root / "_internal" / "_ssl.cpython-312-darwin.so")
    (root / "_internal" / "base_library.zip").write_bytes(b"PK\x03\x04")
    ent = tmp_path / "entitlements.plist"

    plan = bs.codesign_plan(root, exe, "Developer ID Application: X (T)", ent)
    targets = [call[-1] for call in plan]
    assert targets == [str(deep), str(ext), str(exe)]
    for call in plan:
        assert call[:4] == ["codesign", "--force", "--sign", "Developer ID Application: X (T)"]
        assert "--options" in call and "runtime" in call and "--timestamp" in call
    assert plan[-1][-3:] == ["--entitlements", str(ent), str(exe)]
    assert "--entitlements" not in plan[0]

    adhoc = bs.codesign_plan(root, exe, "-", ent)
    assert all("--timestamp" not in call and "runtime" not in call for call in adhoc)
    assert adhoc[-1][-3:] == ["--entitlements", str(ent), str(exe)]


def test_the_cli_link_verb_is_bundled(bs):
    assert "infra.cli_link" in bs.CLI_FORWARD_MODULES
