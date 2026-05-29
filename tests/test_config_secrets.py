"""Secrets overlay — model API key + A2A bearer must never land in the
tracked config YAML.

Invariants:
- ``split_secret_updates`` pulls secret fields out of the main config and
  keeps only non-blank values on the secret side (blank = leave unchanged).
- ``strip_secrets_from_doc`` scrubs secrets an older YAML still carries.
- ``save_secrets`` merges (doesn't clobber siblings) and writes 0600.
- ``LangGraphConfig.from_yaml`` overlays the secrets file, and a blank
  overlay still leaves the env fallback intact.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def test_split_extracts_secrets_and_drops_blanks() -> None:
    from graph.config_io import split_secret_updates

    main, secrets = split_secret_updates(
        {
            "model": {"name": "m", "api_base": "http://x", "api_key": "sk-live"},
            "auth": {"token": ""},  # blank → leave unchanged
            "identity": {"name": "a"},
        }
    )

    # secret pulled out of main, non-secret model fields preserved
    assert "api_key" not in main["model"]
    assert main["model"] == {"name": "m", "api_base": "http://x"}
    assert main["identity"] == {"name": "a"}
    # blank auth.token dropped entirely (no empty section left behind)
    assert "auth" not in main
    assert secrets == {"model": {"api_key": "sk-live"}}


def test_split_does_not_mutate_input() -> None:
    from graph.config_io import split_secret_updates

    original = {"model": {"api_key": "sk-x", "name": "m"}}
    split_secret_updates(original)
    assert original["model"]["api_key"] == "sk-x"  # deep-copied, untouched


def test_strip_secrets_from_doc_scrubs_existing_key() -> None:
    from graph.config_io import strip_secrets_from_doc

    doc = {"model": {"name": "m", "api_key": "sk-leftover"}, "auth": {"token": "t"}}
    strip_secrets_from_doc(doc)
    assert doc["model"] == {"name": "m"}
    assert "auth" not in doc  # emptied section removed


def test_save_and_load_secrets_round_trip(monkeypatch, tmp_path: Path) -> None:
    from graph import config_io

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setattr(config_io, "SECRETS_YAML_PATH", secrets_path)

    config_io.save_secrets({"model": {"api_key": "sk-1"}})
    config_io.save_secrets({"auth": {"token": "bearer-2"}})  # merge, don't clobber

    loaded = config_io.load_secrets()
    assert loaded == {"model": {"api_key": "sk-1"}, "auth": {"token": "bearer-2"}}
    # owner-only perms
    assert stat.S_IMODE(os.stat(secrets_path).st_mode) == 0o600


def test_save_secrets_noop_on_empty(monkeypatch, tmp_path: Path) -> None:
    from graph import config_io

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setattr(config_io, "SECRETS_YAML_PATH", secrets_path)
    config_io.save_secrets({})
    assert not secrets_path.exists()


def test_from_yaml_overlays_secrets_file(tmp_path: Path) -> None:
    from graph.config import LangGraphConfig

    (tmp_path / "langgraph-config.yaml").write_text(
        "model:\n  name: m\n  api_key: \"\"\nauth:\n  token: \"\"\n"
    )
    (tmp_path / "secrets.yaml").write_text(
        "model:\n  api_key: sk-from-overlay\nauth:\n  token: bearer-overlay\n"
    )

    cfg = LangGraphConfig.from_yaml(tmp_path / "langgraph-config.yaml")
    assert cfg.api_key == "sk-from-overlay"
    assert cfg.auth_token == "bearer-overlay"


def test_from_yaml_without_secrets_leaves_blank_for_env_fallback(tmp_path: Path) -> None:
    # No secrets.yaml and a blank YAML key → config stays "" so create_llm /
    # set_a2a_token fall back to OPENAI_API_KEY / A2A_AUTH_TOKEN.
    from graph.config import LangGraphConfig

    (tmp_path / "langgraph-config.yaml").write_text("model:\n  name: m\n  api_key: \"\"\n")
    cfg = LangGraphConfig.from_yaml(tmp_path / "langgraph-config.yaml")
    assert cfg.api_key == ""
