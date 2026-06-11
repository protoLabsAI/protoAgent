"""Fleet-member config scoping — the double-scope regression (ADR 0042 / 0004).

A member is launched with BOTH ``PROTOAGENT_CONFIG_DIR=<ws>`` AND
``PROTOAGENT_INSTANCE=<id>``. ``config_io`` must NOT ``scope_leaf`` the config /
secrets under the already-per-member dir — doing so double-nests
``<ws>/<id>/secrets.yaml``, so a saved plugin secret reads back ``unset`` because
the plugin actually lives at ``<ws>/plugins`` (``PROTOAGENT_PLUGINS_DIR``), not
``<ws>/<id>/plugins``. See ``graph/config_io._config_scope``.
"""

from __future__ import annotations


def test_config_scope_skips_scope_leaf_when_config_dir_is_explicit(monkeypatch, tmp_path):
    """An explicit PROTOAGENT_CONFIG_DIR is already the isolated leaf — config /
    secrets sit directly under it, NOT under a second <instance>/ level."""
    monkeypatch.setenv("PROTOAGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "member-7")
    from graph.config_io import _config_scope

    p = tmp_path / "secrets.yaml"
    assert _config_scope(p) == p  # unchanged — no <member-7>/ nesting


def test_config_scope_still_isolates_a_shared_default_dir(monkeypatch, tmp_path):
    """Without an explicit dir, co-located instances DO isolate by instance id
    under the shared default dir (the legacy scope_leaf path stays intact)."""
    monkeypatch.delenv("PROTOAGENT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "member-7")
    from graph.config_io import _config_scope

    p = tmp_path / "secrets.yaml"
    assert _config_scope(p) == tmp_path / "member-7" / "secrets.yaml"


def test_plugin_secret_resolves_when_config_dir_holds_the_plugins(tmp_path):
    """The regression itself: a plugin secret in ``<dir>/secrets.yaml`` merges into
    ``plugin_config`` when the plugin lives at ``<dir>/plugins`` (config_dir ==
    plugins dir). The double-scope bug pointed config_dir at a nested dir with no
    plugins, so ``_resolve_plugin_config`` found nothing and silently produced
    ``is_set=False`` even though the secret was on disk."""
    pdir = tmp_path / "plugins" / "demo"
    pdir.mkdir(parents=True)
    (pdir / "protoagent.plugin.yaml").write_text(
        "id: demo\nname: Demo\nversion: 0.1.0\nconfig_section: demo\n"
        "secrets: [api_key]\n"
        "settings:\n  - { key: api_key, label: Key, type: secret }\n"
    )
    (pdir / "__init__.py").write_text("def register(registry):\n    pass\n")
    (tmp_path / "langgraph-config.yaml").write_text("plugins:\n  enabled: [demo]\n")
    (tmp_path / "secrets.yaml").write_text("demo:\n  api_key: super-secret-token\n")

    from graph.config import LangGraphConfig

    cfg = LangGraphConfig.from_yaml(tmp_path / "langgraph-config.yaml")
    assert cfg.plugin_config.get("demo", {}).get("api_key") == "super-secret-token"


def test_self_heal_removes_orphaned_double_scoped_dir(monkeypatch, tmp_path):
    """On startup a member with an explicit config dir + an OLD nested config drops
    the orphaned ``<dir>/<id>/`` dir (reset — by design we don't migrate it)."""
    from pathlib import Path

    import graph.config_io as cio

    nested = tmp_path / "member-7"
    nested.mkdir()
    (nested / "langgraph-config.yaml").write_text("plugins: {enabled: []}\n")
    (nested / "secrets.yaml").write_text("model: {api_key: x}\n")

    monkeypatch.setenv("PROTOAGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cio, "_BASE_CONFIG_YAML", tmp_path / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "CONFIG_YAML_PATH", tmp_path / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "_scope_leaf", lambda p: nested / Path(p).name)

    cio._reset_double_scoped_config()
    assert not nested.exists()  # orphan removed


def test_self_heal_is_a_noop_without_an_explicit_config_dir(monkeypatch, tmp_path):
    """The default (shared-dir) case is legitimately scoped — never reset it."""
    import graph.config_io as cio

    monkeypatch.delenv("PROTOAGENT_CONFIG_DIR", raising=False)
    nested = tmp_path / "member-7"
    nested.mkdir()
    (nested / "langgraph-config.yaml").write_text("x\n")
    monkeypatch.setattr(cio, "_scope_leaf", lambda p: nested / "langgraph-config.yaml")
    cio._reset_double_scoped_config()
    assert nested.exists()  # untouched
