"""Regression tests for scripts/gen_attribution.py — specifically the
carry-forward + guard behavior that keeps a bare-env regen from wiping the
Python license column to UNKNOWN (issue #2047).

The license *inventory* (name==version) is already a deterministic function of
the lockfiles and covered by the --check gate; what these tests pin down is the
license *column*, which resolves from the installed environment and used to
silently degrade when regenerated somewhere the deps weren't importable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gen_attribution.py"


@pytest.fixture()
def gen(monkeypatch, tmp_path):
    """Load gen_attribution fresh and point every filesystem global at a temp
    sandbox so tests never read or write the real repo files."""
    spec = importlib.util.spec_from_file_location("gen_attribution_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "OUT", tmp_path / "THIRD_PARTY_LICENSES.md")
    monkeypatch.setattr(mod, "UV_LOCK", tmp_path / "uv.lock")
    monkeypatch.setattr(mod, "NPM_LOCK", tmp_path / "package-lock.json")
    # npm licenses come from the lockfile (deterministic) and aren't what these
    # tests exercise — stub the whole collector out.
    monkeypatch.setattr(mod, "collect_npm", lambda: [])
    return mod


def _write_lock(gen, *pkgs: tuple[str, str]) -> None:
    body = "\n".join(f'[[package]]\nname = "{n}"\nversion = "{v}"\n' for n, v in pkgs)
    gen.UV_LOCK.write_text(body, encoding="utf-8")


def _write_manifest(gen, *rows: tuple[str, str, str]) -> None:
    lines = ["| Package | Version | License |", "| --- | --- | --- |"]
    lines += [f"| `{n}` | {v} | {lic} |" for n, v, lic in rows]
    gen.OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _env(gen, monkeypatch, mapping: dict[str, str]) -> None:
    """Fake the installed-environment license resolution (keyed by canon name)."""
    monkeypatch.setattr(
        gen, "_installed_py_licenses", lambda: {gen._canon(k): v for k, v in mapping.items()}
    )


def test_committed_licenses_parses_only_package_rows(gen):
    _write_manifest(gen, ("foo", "1.2.3", "MIT"), ("bar-baz", "4.5.6", "BSD-3-Clause"))
    assert gen._committed_licenses() == {
        ("foo", "1.2.3"): "MIT",
        ("bar-baz", "4.5.6"): "BSD-3-Clause",
    }


def test_committed_licenses_missing_file_is_empty(gen):
    assert not gen.OUT.exists()
    assert gen._committed_licenses() == {}


def test_env_resolution_wins_and_carries_nothing(gen, monkeypatch):
    _write_lock(gen, ("foo", "1.2.3"))
    _write_manifest(gen, ("foo", "1.2.3", "Apache-2.0"))  # stale prior value
    _env(gen, monkeypatch, {"foo": "MIT License"})  # env resolves it
    rows, carried = gen.collect_python()
    assert rows == [{"name": "foo", "version": "1.2.3", "license": "MIT"}]  # normalized
    assert carried == []  # env resolved → no carry-forward


def test_carry_forward_preserves_immutable_license_when_env_blank(gen, monkeypatch):
    _write_lock(gen, ("foo", "1.2.3"))
    _write_manifest(gen, ("foo", "1.2.3", "MIT"))
    _env(gen, monkeypatch, {})  # bare env resolves nothing
    rows, carried = gen.collect_python()
    assert rows == [{"name": "foo", "version": "1.2.3", "license": "MIT"}]
    assert carried == ["foo==1.2.3"]


def test_no_carry_when_prior_version_differs(gen, monkeypatch):
    """A carry is keyed on the exact name==version; a bumped version can't
    inherit the old release's license."""
    _write_lock(gen, ("foo", "2.0.0"))
    _write_manifest(gen, ("foo", "1.2.3", "MIT"))  # only the old version recorded
    _env(gen, monkeypatch, {})
    rows, carried = gen.collect_python()
    assert rows == [{"name": "foo", "version": "2.0.0", "license": "UNKNOWN"}]
    assert carried == []


def test_write_guard_refuses_all_unknown_seed(gen, monkeypatch):
    """Seeding in a bare env with no prior file to carry from must refuse rather
    than commit an all-UNKNOWN manifest that would still pass --check."""
    _write_lock(gen, ("foo", "1.2.3"), ("bar", "4.5.6"))
    _env(gen, monkeypatch, {})  # nothing resolves, nothing to carry
    monkeypatch.setattr("sys.argv", ["gen_attribution.py"])
    assert gen.main() == 1
    assert not gen.OUT.exists()  # left untouched


def test_bare_env_regen_preserves_and_writes(gen, monkeypatch):
    """The #2047 scenario: bare-env regen with the manifest present preserves
    every license and still writes (guard not tripped)."""
    _write_lock(gen, ("foo", "1.2.3"), ("bar", "4.5.6"))
    _write_manifest(gen, ("foo", "1.2.3", "MIT"), ("bar", "4.5.6", "BSD-3-Clause"))
    _env(gen, monkeypatch, {})  # bare env
    monkeypatch.setattr("sys.argv", ["gen_attribution.py"])
    assert gen.main() == 0
    written = gen.OUT.read_text("utf-8")
    assert "| `foo` | 1.2.3 | MIT |" in written
    assert "| `bar` | 4.5.6 | BSD-3-Clause |" in written
    assert "UNKNOWN (2)" not in written  # nothing degraded
