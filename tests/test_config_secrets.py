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
    monkeypatch.setattr(config_io, "secrets_yaml_path", lambda: secrets_path)

    config_io.save_secrets({"model": {"api_key": "sk-1"}})
    config_io.save_secrets({"auth": {"token": "bearer-2"}})  # merge, don't clobber

    loaded = config_io.load_secrets()
    assert loaded == {"model": {"api_key": "sk-1"}, "auth": {"token": "bearer-2"}}
    # owner-only perms
    assert stat.S_IMODE(os.stat(secrets_path).st_mode) == 0o600


def test_save_secrets_noop_on_empty(monkeypatch, tmp_path: Path) -> None:
    from graph import config_io

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setattr(config_io, "secrets_yaml_path", lambda: secrets_path)
    config_io.save_secrets({})
    assert not secrets_path.exists()


def test_from_yaml_overlays_secrets_file(tmp_path: Path) -> None:
    from graph.config import LangGraphConfig

    (tmp_path / "langgraph-config.yaml").write_text('model:\n  name: m\n  api_key: ""\nauth:\n  token: ""\n')
    (tmp_path / "secrets.yaml").write_text("model:\n  api_key: sk-from-overlay\nauth:\n  token: bearer-overlay\n")

    cfg = LangGraphConfig.from_yaml(tmp_path / "langgraph-config.yaml")
    assert cfg.api_key == "sk-from-overlay"
    assert cfg.auth_token == "bearer-overlay"


def test_config_path_honors_home_override(monkeypatch, tmp_path: Path) -> None:
    # The desktop sidecar points PROTOAGENT_HOME at a writable app-data dir so a
    # read-only frozen binary can still persist setup — config lands under
    # <home>/config (the instance root IS that dir).
    from graph import config_io

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "appdata"))
    assert config_io.config_yaml_path() == tmp_path / "appdata" / "config" / "langgraph-config.yaml"
    assert config_io.secrets_yaml_path() == tmp_path / "appdata" / "config" / "secrets.yaml"


def test_from_yaml_without_secrets_leaves_blank_for_env_fallback(tmp_path: Path) -> None:
    # No secrets.yaml and a blank YAML key → config stays "" so create_llm /
    # set_a2a_token fall back to OPENAI_API_KEY / A2A_AUTH_TOKEN.
    from graph.config import LangGraphConfig

    (tmp_path / "langgraph-config.yaml").write_text('model:\n  name: m\n  api_key: ""\n')
    cfg = LangGraphConfig.from_yaml(tmp_path / "langgraph-config.yaml")
    assert cfg.api_key == ""


def test_disabled_plugin_secret_routes_to_secrets_not_plaintext(tmp_path, monkeypatch) -> None:
    """A secret saved for an installed-but-DISABLED plugin must still be pulled into the
    secret half (→ secrets.yaml), never left in the plaintext live config. The routing
    (`secret_paths`) covers ALL installed plugins, not just enabled ones — otherwise a
    plugin that's off (or being configured before enable) leaks its key to the config."""
    from graph.config_io import split_secret_updates

    cfg = tmp_path / "cfg"
    pdir = cfg / "plugins" / "offp"
    pdir.mkdir(parents=True)
    (pdir / "protoagent.plugin.yaml").write_text(
        "id: offp\nname: Off Plugin\nversion: 0.1.0\nconfig_section: offp\nsecrets: [api_key]\n"
    )
    (pdir / "__init__.py").write_text("def register(registry):\n    pass\n")
    (cfg / "langgraph-config.yaml").write_text("plugins:\n  enabled: []\n")  # offp NOT enabled
    monkeypatch.setenv("PROTOAGENT_HOME", str(cfg))  # instance_paths().plugins_dir == cfg/plugins

    main, secrets = split_secret_updates({"offp": {"api_key": "sek-ret"}})
    assert secrets == {"offp": {"api_key": "sek-ret"}}  # routed to the secret half
    assert "offp" not in main  # NOT left in the plaintext config YAML


# ── #877: a plugin secret-path discovery failure must fail SAFE, not empty ──────


def test_secret_paths_falls_back_to_cache_on_discovery_failure(monkeypatch, caplog):
    """If plugin secret-path discovery raises, secret_paths() keeps the last
    successfully-discovered plugin secrets (cached) rather than dropping to the base
    set — otherwise strip_secrets_from_doc would let that key reach the main YAML."""
    import logging

    from graph import config_io

    pair = ("myplugin", "api_key")

    class _Schema:
        section = "myplugin"
        secrets = ["api_key"]

    # 1) a good discovery populates the cache (the plugin secret is recognized).
    monkeypatch.setattr(config_io, "_PLUGIN_SECRET_PATHS_CACHE", ())
    monkeypatch.setattr("graph.plugins.pconfig.installed_plugin_config_schemas", lambda **kw: [_Schema()])
    assert pair in config_io.secret_paths()

    # 2) discovery now FAILS — the plugin secret must still be recognized (cached),
    #    and the failure is logged (no silent downgrade).
    def _boom(**kw):
        raise RuntimeError("manifest parse blew up")

    monkeypatch.setattr("graph.plugins.pconfig.installed_plugin_config_schemas", _boom)
    with caplog.at_level(logging.WARNING, logger="protoagent.config_io"):
        paths = config_io.secret_paths()
    assert pair in paths  # fail-safe: NOT dropped to the base set
    assert any("secret-path discovery failed" in r.message for r in caplog.records)


def test_a_plugin_secret_is_stripped_from_the_doc_even_if_rediscovery_fails(monkeypatch):
    """End-to-end of the #877 guarantee: once a plugin secret is known, a later
    discovery failure doesn't let strip_secrets_from_doc leave it in the main YAML."""
    from graph import config_io

    class _Schema:
        section = "myplugin"
        secrets = ["api_key"]

    monkeypatch.setattr(config_io, "_PLUGIN_SECRET_PATHS_CACHE", ())
    monkeypatch.setattr("graph.plugins.pconfig.installed_plugin_config_schemas", lambda **kw: [_Schema()])
    config_io.secret_paths()  # prime the cache

    monkeypatch.setattr(
        "graph.plugins.pconfig.installed_plugin_config_schemas",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    doc = {"myplugin": {"api_key": "sk-should-be-stripped", "host": "ok-to-keep"}}
    config_io.strip_secrets_from_doc(doc)
    assert "api_key" not in doc["myplugin"]  # the secret never reaches the main YAML
    assert doc["myplugin"]["host"] == "ok-to-keep"  # non-secret kept


def test_config_to_dict_blanks_plugin_section_on_discovery_failure(monkeypatch):
    """GET /api/config must fail SAFE: if plugin-schema discovery raises, config_to_dict
    can't tell a secret from a non-secret value, so it blanks the WHOLE plugin section
    rather than echoing a plugin secret in the clear."""
    from graph import config_io
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig(plugin_config={"discord": {"bot_token": "sek-ret", "guild": "123"}})

    class _S:
        section = "discord"
        secrets = ["bot_token"]

    # discovery OK → only the declared secret is blanked, non-secret config preserved.
    monkeypatch.setattr("graph.plugins.pconfig.installed_plugin_config_schemas", lambda **kw: [_S()])
    d = config_io.config_to_dict(cfg)
    assert d["discord"]["bot_token"] == ""
    assert d["discord"]["guild"] == "123"

    # discovery FAILS → blank the whole section (no plugin secret echoed in the clear).
    monkeypatch.setattr(
        "graph.plugins.pconfig.installed_plugin_config_schemas",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("discovery boom")),
    )
    d2 = config_io.config_to_dict(cfg)
    assert d2["discord"] == {"bot_token": "", "guild": ""}
