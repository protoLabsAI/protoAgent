"""Seam guard for the ``graph/config_load.py`` extraction (#3840).

The loader helpers moved out of ``graph/config.py`` and are re-exported there, so
``graph.config.<name>`` still RESOLVES. Whether a patch INTERCEPTS depends on where the
caller looks the name up:

* ``_load_host_layer`` / ``_resolve_plugin_config`` are patched on ``graph.config``
  (``from_dict`` calls the latter by bare name in graph/config.py; ``_read_config_docs``
  calls the former THROUGH ``graph.config`` at call time), so those patches stay live —
  and a patch on ``graph.config_load`` for them would be dead.
* Every other moved collaborator is called by bare name only from inside config_load,
  so a patch on ``graph.config`` for it would be dead: patch ``graph.config_load``.

This scans the suite so a stale target fails loudly instead of passing against the real thing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import graph.config as config
import graph.config_load as config_load

# Patched on graph.config — the ONLY module whose binding the live callers read.
_PATCH_ON_CONFIG = {"_load_host_layer", "_resolve_plugin_config"}

# Called (by bare name) only from inside config_load — a patch on graph.config misses them.
_PATCH_ON_CONFIG_LOAD = {
    "_load_secrets_doc",
    "_drop_host_model_identity",
    "_warn_shadowed_host_keys",
    "_deep_merge_dicts",
    "_merge_provider_lists",
    "_filter_to_host_keys",
    "_host_scoped_fields",
}

_RE_EXPORTED = (
    "SECRETS_FILENAME",
    "_load_secrets_doc",
    "_MODEL_IDENTITY_KEYS",
    "_drop_host_model_identity",
    "_read_config_docs",
    "load_config_docs",
    "_hydrate_external_secrets",
    "_resolve_plugin_config",
    "_deep_merge_dicts",
    "_get_dotted",
    "_set_dotted",
    "_env_default",
    "_FALSE_STRINGS",
    "_OFFICIAL_SOURCES_DEFAULT",
    "_coerce_budget_pct",
    "_coerce_prior_sessions",
    "_coerce_digest_cap",
    "yaml_word_for_bool",
    "_falsey",
    "_valid_a2a_skills",
    "_default_filesystem_allow_run",
    "_parse_sources_allow",
    "_default_prompt_cache_ttl",
    "_host_scoped_fields",
    "_merge_provider_lists",
    "_filter_to_host_keys",
    "_warn_shadowed_host_keys",
    "_load_host_layer",
)

_TESTS = Path(__file__).resolve().parent


def _module_aliases(tree: ast.AST, dotted: str) -> set[str]:
    pkg, _, leaf = dotted.rpartition(".")
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == dotted and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == pkg:
            aliases |= {a.asname or a.name for a in node.names if a.name == leaf}
    return aliases


def test_no_test_patches_a_moved_name_on_the_wrong_module():
    wrong_module = {"graph.config": _PATCH_ON_CONFIG_LOAD, "graph.config_load": _PATCH_ON_CONFIG}
    stale: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {mod: _module_aliases(tree, mod) for mod in wrong_module}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"setattr", "object", "patch"}:
                continue
            first = node.args[0]
            for mod, dead in wrong_module.items():
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    owner, _, attr = first.value.rpartition(".")
                    if owner == mod and attr in dead:
                        stale.append(f"{path.name}:{node.lineno} {first.value}")
                elif (
                    isinstance(first, ast.Name)
                    and first.id in aliases[mod]
                    and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in dead
                ):
                    stale.append(f"{path.name}:{node.lineno} {mod}.{node.args[1].value}")
    assert not stale, "these patches cannot intercept the live caller (#3840): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    """graph.config re-exports every moved name by identity — no stale copies, and the
    module-level constants have ONE home."""
    for name in _RE_EXPORTED:
        assert getattr(config, name) is getattr(config_load, name), name


def test_read_config_docs_resolves_load_host_layer_through_graph_config(monkeypatch, tmp_path):
    """``_read_config_docs`` (moved) looks ``_load_host_layer`` up on graph.config at call
    time, so the suite's ``patch("graph.config._load_host_layer")`` intercepts it."""
    monkeypatch.setattr(config, "_load_host_layer", lambda: {"model": {"api_base": "http://host.example/v1"}})
    merged, _secrets, present = config_load._read_config_docs(tmp_path / "absent.yaml")
    assert present and merged["model"]["api_base"] == "http://host.example/v1"


def test_from_dict_resolves_resolve_plugin_config_through_graph_config(monkeypatch):
    """``from_dict`` stays in graph/config.py and calls ``_resolve_plugin_config`` by bare
    name — the binding a patch on graph.config replaces."""
    monkeypatch.setattr(config, "_resolve_plugin_config", lambda *a, **k: {"sentinel": {"k": 1}})
    assert config.LangGraphConfig.from_dict({}).plugin_config == {"sentinel": {"k": 1}}
