"""Text IO must name its encoding — the #2521 class, and the gate that ends it.

Python's text mode defaults to the locale code page, which is CP1252 on a Western
Windows install and UTF-8 nearly everywhere else. Every occurrence of that default in a
path that reads or writes operator data has produced a user-visible bug:

- #2521 — configs and registries written as CP1252, then crashing a strict UTF-8 reader;
- #2520 — a config READ decoding "Café" as "CafÃ©", which then reached the agent switcher
  and the Fleet API double-encoded;
- the friction ledger mojibaking its own summaries;
- #2586 / #2596 — the newline half of the same defect.

Each was fixed one call site at a time. These tests pin the two things that stop the next
one: the specific chain that produced #2520, and the lint rule that now fails the build.
"""

from __future__ import annotations

import builtins
import pathlib

import pytest


@pytest.fixture
def windows_default_encoding(monkeypatch):
    """Force encoding-less text opens to CP1252, the way Windows does.

    The bug is invisible on macOS/Linux, where the locale default is already UTF-8 — a
    green run there proves nothing. This makes the Windows condition reproducible
    anywhere, so the regression is caught by the CI that runs on every PR rather than by
    a Windows user months later.
    """
    real_open = builtins.open

    def _cp1252_open(file, mode="r", *args, **kwargs):
        if "b" not in mode and "encoding" not in kwargs:
            kwargs["encoding"] = "cp1252"
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _cp1252_open)


def test_config_read_keeps_utf8_under_a_windows_default(windows_default_encoding, tmp_path):
    """#2520, end to end. The config file holds correct UTF-8; reading it with the locale
    default produced 'CafÃ©', which flowed into identity.name → the fleet label → the
    switcher and /api/fleet, where it was re-encoded into the double sequence Dennis
    captured (43 61 66 C3 83 C2 A9)."""
    from graph.config import _read_config_docs

    cfg = tmp_path / "langgraph-config.yaml"
    cfg.write_bytes("identity:\n  name: PA Windows Lifecycle Café\n".encode())

    merged, _secrets, present = _read_config_docs(cfg)

    assert present
    assert merged["identity"]["name"] == "PA Windows Lifecycle Café"
    assert "Ã" not in merged["identity"]["name"]  # the exact reported mojibake


def test_secrets_read_keeps_utf8_under_a_windows_default(windows_default_encoding, tmp_path):
    """The sibling read one function up — a passphrase or token with a non-ASCII
    character would have been silently mangled into a credential that doesn't match."""
    from graph.config import _load_secrets_doc

    (tmp_path / "secrets.yaml").write_bytes("model:\n  api_key: clé-secrète\n".encode())

    assert _load_secrets_doc(tmp_path)["model"]["api_key"] == "clé-secrète"


def test_the_lint_gate_stays_on():
    """PLW1514 is what makes this a class fix rather than four one-off patches. It is a
    preview rule, so it needs `preview` + `explicit-preview-rules` to be enabled without
    dragging in every other unstable rule — if any of the three is dropped, the guard is
    silently gone and only this test notices."""
    import tomllib

    root = pathlib.Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as fh:
        lint = tomllib.load(fh)["tool"]["ruff"]["lint"]

    assert "PLW1514" in lint["select"], "the unspecified-encoding rule must stay selected"
    assert lint.get("preview") is True, "PLW1514 is preview-only — it does nothing without this"
    assert lint.get("explicit-preview-rules") is True, "keep preview scoped to the rules we name"
    assert "PLW1514" not in lint.get("ignore", []), "re-ignoring it would undo the sweep silently"
