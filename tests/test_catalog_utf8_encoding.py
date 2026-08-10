"""Repo-authored catalogs must be read as UTF-8, not the process locale (#2464).

On Windows, ``Path.read_text()`` with no encoding uses the active locale codepage
(cp1252, cp932, …), while every catalog this repo ships is UTF-8 with real
punctuation in it. The v0.130.0 Windows acceptance run surfaced this as mojibake:
``/api/archetypes`` served ``â€”`` where the catalog says ``—``. Worse than
mojibake: under a multibyte locale (cp932) the decode can raise
``UnicodeDecodeError``, which the readers' ``(JSONDecodeError, OSError)`` guards
do not catch.

Files protoAgent writes itself are ASCII-escaped by ``json.dump`` and don't bite;
it is exactly the repo/bundle-authored catalogs that carry non-ASCII. Hence the
sweep below is scoped to ``operator_api/``, where those catalogs are read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def windows_locale_read_text(monkeypatch):
    """Simulate a Windows cp1252 locale: bare ``read_text()`` decodes with the
    locale codepage instead of UTF-8, exactly like the frozen Windows build."""
    real_read_text = Path.read_text

    def locale_read_text(self, encoding=None, errors=None, newline=None):
        if encoding is None:
            return self.read_bytes().decode("cp1252", errors=errors or "strict")
        return real_read_text(self, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", locale_read_text)


def test_archetype_catalog_survives_non_utf8_locale(
    tmp_path, monkeypatch, windows_locale_read_text
):
    from infra.paths import InstancePaths

    ip = InstancePaths(
        instance_id="t",
        box_root=tmp_path / "box",
        instance_root=tmp_path / "inst",
        app_root=tmp_path / "app",
    )
    ip.config_dir.mkdir(parents=True)
    catalog = {"archetypes": [{"id": "em", "title": "Coach — em dash intact"}]}
    (ip.config_dir / "archetype-catalog.json").write_bytes(
        json.dumps(catalog, ensure_ascii=False).encode("utf-8")
    )
    monkeypatch.setattr("infra.paths.instance_paths", lambda: ip)

    from operator_api.fleet_routes import _load_archetype_catalog

    entries = _load_archetype_catalog()
    assert entries[0]["title"] == "Coach — em dash intact"


def test_shipped_catalogs_actually_exercise_the_regression():
    """The behavioral test above is only meaningful while the shipped catalogs
    carry non-ASCII text. If this ever fails, the catalogs went ASCII-only and
    the locale test needs a new canary."""
    shipped = list((ROOT / "config").glob("*catalog*.json"))
    assert shipped, "no shipped catalogs found under config/"
    assert any(
        any(ord(ch) > 127 for ch in f.read_text(encoding="utf-8")) for f in shipped
    ), "no shipped catalog contains non-ASCII — locale regression no longer exercised"


def test_no_bare_read_text_in_operator_api():
    """Sweep: every ``read_text`` in operator_api/ names an encoding, so the next
    catalog reader added can't silently reintroduce the locale dependency."""
    offenders = []
    for py in (ROOT / "operator_api").rglob("*.py"):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.read_text\(\s*\)", line):
                offenders.append(f"{py.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "bare read_text() decodes with the Windows locale codepage — pass "
        f"encoding='utf-8' explicitly: {offenders}"
    )
