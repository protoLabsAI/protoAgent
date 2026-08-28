"""The desktop bundles a PINNED `br` (beads-rust) sidecar — #3236.

The project_board plugin resolves the beads CLI as: explicit BR_BIN env > its own
fetched binary > `br` on PATH — and auto-fetches ONLY when none resolve. On
desktop that fetch is the wrong story (a fresh install downloading a binary at
first board use), so build_sidecar.py fetches the PINNED release at BUILD time
into `binaries/br-<target-triple>`, Tauri bundles it as a second externalBin, and
the shell (lib.rs) hands its installed path to the server as BR_BIN — the
plugin's own precedence then makes the distributed binary win, its status reports
br.source "env", and no fetch ever starts.

The pin's single source is the PLUGIN's br_fetch.py (BR_VERSION + BR_SHA256),
loaded at build time (a PROJECT_BOARD_SRC checkout, else the plugin repo's raw
file) — never copied into this repo, so the two cannot drift. These tests drive
build_sidecar's br machinery with a FAKE br_fetch module (no network, no plugin
checkout — the main suite must pass without either); the desktop-build workflow's
"Verify the bundled br matches the plugin pin" step asserts the real fetched
binary reports the real pin. Windows is out of scope BY DESIGN: no pin exists,
no br is bundled (tauri.windows.conf.json drops the externalBin — JSON merge
patch replaces arrays), and the plugin's manual-install hint still applies there.
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "apps" / "desktop" / "sidecar" / "build_sidecar.py"
TAURI = ROOT / "apps" / "desktop" / "src-tauri"


@pytest.fixture(scope="module")
def bs():
    """build_sidecar, imported from its file (apps/ is not a package). Module
    level is constants + defs only; main() is __main__-guarded."""
    spec = importlib.util.spec_from_file_location("build_sidecar_under_test", SIDECAR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _boom(*_a, **_k):
    raise AssertionError("load_br_fetch must not be called on this path — it would hit the network")


def _fake_br_fetch(version: str = "9.9.9", platforms: tuple[str, ...] = ("darwin_arm64",)):
    """A stand-in for the plugin's br_fetch module: fetch_spec honours the
    platform pins, fetch_br writes the file and records the call."""
    calls: list[tuple[object, Path]] = []

    def fetch_spec(platform=None):
        if platform not in platforms:
            return None
        return SimpleNamespace(
            version=version,
            platform=platform,
            url=f"https://example.invalid/br-{version}-{platform}.tar.gz",
            sha256="0" * 64,
        )

    def fetch_br(spec, dest, **_kw):
        calls.append((spec, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"#!fake-br")
        return dest

    return types.SimpleNamespace(BR_VERSION=version, fetch_spec=fetch_spec, fetch_br=fetch_br, calls=calls)


def _fake_run(stdout: str):
    def run(_argv, **_kw):
        return SimpleNamespace(stdout=stdout, returncode=0)

    return run


def test_unpinned_targets_are_not_in_the_triple_map(bs) -> None:
    """Windows and musl stay out of scope — the plugin has no sha256 pin for
    them, so the desktop must not pretend to bundle one (r3)."""
    for triple in (
        "x86_64-pc-windows-msvc",
        "aarch64-pc-windows-msvc",
        "x86_64-unknown-linux-musl",
    ):
        assert triple not in bs.BR_TRIPLE_PLATFORMS


def test_windows_target_skips_without_touching_the_pin_source(bs, tmp_path, monkeypatch) -> None:
    """The Windows leg runs build_sidecar too: it must skip the br fetch cleanly
    (a note, not an error) and never even LOAD br_fetch (a network hit)."""
    monkeypatch.setattr(bs, "load_br_fetch", _boom)
    assert bs.fetch_br_sidecar("x86_64-pc-windows-msvc", tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_skip_env_short_circuits_the_fetch(bs, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(bs.ENV_SKIP_BR, "1")
    monkeypatch.setattr(bs, "load_br_fetch", _boom)
    assert bs.fetch_br_sidecar("aarch64-apple-darwin", tmp_path) is None


def test_fetch_lands_at_br_target_triple_and_uses_the_plugin_pin(bs, tmp_path) -> None:
    """The dest is the exact name Tauri looks for (`br-<triple>`), the platform
    key comes from the triple map, and the spec is the PLUGIN's — build_sidecar
    never invents a version or checksum of its own (r2's single-source half)."""
    fake = _fake_br_fetch(version="9.9.9")
    dest = bs.fetch_br_sidecar(
        "aarch64-apple-darwin", tmp_path, br_fetch=fake, run=_fake_run("br 9.9.9\n")
    )
    assert dest == tmp_path / "br-aarch64-apple-darwin"
    assert dest.is_file()
    spec, fetched_dest = fake.calls[0]
    assert spec.platform == "darwin_arm64"
    assert spec.version == fake.BR_VERSION
    assert fetched_dest == dest


def test_a_binary_reporting_the_wrong_version_fails_the_build(bs, tmp_path) -> None:
    """r2's other half: the bundled binary must REPORT br_fetch.BR_VERSION —
    alias drift or a bad release asset dies at build time, never in a bundle."""
    fake = _fake_br_fetch(version="9.9.9")
    with pytest.raises(SystemExit, match="9.9.9"):
        bs.fetch_br_sidecar("aarch64-apple-darwin", tmp_path, br_fetch=fake, run=_fake_run("br 1.0.0\n"))


def test_a_version_superstring_is_not_the_pin(bs, tmp_path) -> None:
    """The review trap (#3236): the pin is a SUBSTRING of `1.2.30`, `11.2.3`
    and `1.2.3.4`, so a containment check would ship a DIFFERENT release. The
    gate must demand the pin as a whole token — every superstring fails."""
    fake = _fake_br_fetch(version="1.2.3")
    for reported in ("br 1.2.30\n", "br 11.2.3\n", "br 1.2.3.4\n", "br 1.2.3-rc1\n", "br 1.2.3rc1\n"):
        with pytest.raises(SystemExit, match="not exactly the pinned"):
            bs.fetch_br_sidecar("aarch64-apple-darwin", tmp_path, br_fetch=fake, run=_fake_run(reported))


def test_br_reports_pin_is_an_exact_token_match(bs) -> None:
    """The one predicate both gates share (fetch_br_sidecar here, the
    desktop-build workflow's verify step in CI): exact version as a whole
    token, with a `v` prefix tolerated as formatting."""
    assert bs.br_reports_pin("br 1.2.3\n", "1.2.3")
    assert bs.br_reports_pin("br v1.2.3 (release build)\n", "1.2.3")
    assert bs.br_reports_pin("beads-rust br version 1.2.3\n", "1.2.3")
    assert not bs.br_reports_pin("br 1.2.30\n", "1.2.3")
    assert not bs.br_reports_pin("br 11.2.3\n", "1.2.3")
    assert not bs.br_reports_pin("br 1.2.3.4\n", "1.2.3")
    assert not bs.br_reports_pin("br 1.2.3-rc1\n", "1.2.3")
    assert not bs.br_reports_pin("br 0.1.2.3\n", "1.2.3")
    assert not bs.br_reports_pin("", "1.2.3")


def test_a_pinless_platform_verdict_from_the_plugin_fails_loudly(bs, tmp_path) -> None:
    """The triple map says a pin should exist; the plugin says it doesn't. That
    is drift between the two repos — fail, don't silently ship no br."""
    fake = _fake_br_fetch(version="9.9.9", platforms=())
    with pytest.raises(SystemExit, match="no pin"):
        bs.fetch_br_sidecar("aarch64-apple-darwin", tmp_path, br_fetch=fake)


def test_load_br_fetch_from_a_local_checkout(bs, tmp_path, monkeypatch) -> None:
    """PROJECT_BOARD_SRC names a plugin checkout (dir or the br_fetch.py file
    itself) — the offline/dev path that never touches the network."""
    (tmp_path / "br_fetch.py").write_text('BR_VERSION = "7.7.7"\n', encoding="utf-8")
    monkeypatch.setenv(bs.ENV_PROJECT_BOARD_SRC, str(tmp_path))
    assert bs.load_br_fetch().BR_VERSION == "7.7.7"
    assert bs.load_br_fetch(str(tmp_path / "br_fetch.py")).BR_VERSION == "7.7.7"


def test_tauri_bundles_br_as_a_second_external_bin() -> None:
    conf = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    assert "binaries/br" in conf["bundle"]["externalBin"]


def test_windows_bundle_excludes_br() -> None:
    """r3: no pin, no binary — the platform config (merged as a JSON merge
    patch, so the array REPLACES) drops br so the Windows `tauri build` never
    looks for a file build_sidecar never makes."""
    conf = json.loads((TAURI / "tauri.windows.conf.json").read_text(encoding="utf-8"))
    ext = conf["bundle"]["externalBin"]
    assert "binaries/protoagent-server" in ext
    assert "binaries/br" not in ext


def test_the_shell_hands_the_bundled_br_to_the_server_as_br_bin() -> None:
    """The lib.rs wiring pin (pytest can't compile Rust): the sidecar spawn env
    must set BR_BIN from the bundled binary — that's what makes the plugin's
    precedence pick the distributed br and report br.source "env" (r1)."""
    src = (TAURI / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "bundled_br_path" in src, "lib.rs lost the bundled-br resolver"
    assert 'env("BR_BIN"' in src, "lib.rs no longer hands BR_BIN to the sidecar spawn env"
