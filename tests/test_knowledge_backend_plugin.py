"""Pluggable knowledge backend — register_knowledge_store + selector (ADR 0031)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from graph.config import LangGraphConfig
from graph.plugins.registry import PluginRegistry
from knowledge import KnowledgeBackend, KnowledgeStore
from server.agent_init import _apply_plugin_knowledge_backend


def _cfg(backend=""):
    c = LangGraphConfig()
    c.knowledge_backend = backend
    return c


class _FakeBackend:
    def add_chunk(self, content, domain="general", **kw): return 1
    def search(self, query, k=5, *, domain=None): return []
    def get_hot_memory(self, max_chars=6000): return ""
    def list_chunks(self, *a, **kw): return []
    def stats(self): return {}
    def delete_by_id(self, chunk_id): return True
    def add_finding(self, *a, **kw): return None


def test_builtin_store_satisfies_protocol(tmp_path):
    store = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    assert isinstance(store, KnowledgeBackend)


def test_registry_register_guards():
    reg = PluginRegistry("p", Path("."))
    reg.register_knowledge_store("pgvector", lambda config: _FakeBackend())
    reg.register_knowledge_store("", lambda config: _FakeBackend())  # bad name → ignored
    reg.register_knowledge_store("x", None)                          # non-callable → ignored
    assert set(reg.knowledge_stores) == {"pgvector"}


def test_config_parses_backend(tmp_path):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"knowledge": {"backend": "pgvector"}}))
    cfg = LangGraphConfig.from_yaml(str(p))
    assert cfg.knowledge_backend == "pgvector"


def test_apply_no_backend_keeps_builtin():
    builtin = object()
    plugins = SimpleNamespace(knowledge_stores={})
    assert _apply_plugin_knowledge_backend(_cfg(""), builtin, plugins) is builtin


def test_apply_selects_registered_backend():
    builtin = object()
    fake = _FakeBackend()
    plugins = SimpleNamespace(knowledge_stores={"pgvector": lambda config: fake})
    assert _apply_plugin_knowledge_backend(_cfg("pgvector"), builtin, plugins) is fake


def test_apply_unregistered_degrades_to_builtin():
    builtin = object()
    plugins = SimpleNamespace(knowledge_stores={})
    assert _apply_plugin_knowledge_backend(_cfg("nope"), builtin, plugins) is builtin


def test_apply_none_or_error_degrades_to_builtin():
    builtin = object()
    def _none(config): return None
    def _boom(config): raise RuntimeError("db down")
    assert _apply_plugin_knowledge_backend(_cfg("b"), builtin, SimpleNamespace(knowledge_stores={"b": _none})) is builtin
    assert _apply_plugin_knowledge_backend(_cfg("b"), builtin, SimpleNamespace(knowledge_stores={"b": _boom})) is builtin


# ── register_embedder (ADR 0031 follow-up) ───────────────────────────────────

def _embed_factory(config):
    return lambda text: [0.0, 1.0, 0.0]


def test_registry_register_embedder_guards():
    reg = PluginRegistry("p", Path("."))
    reg.register_embedder("local", _embed_factory)
    reg.register_embedder("", _embed_factory)   # bad name → ignored
    reg.register_embedder("x", None)            # non-callable → ignored
    assert set(reg.embedders) == {"local"}


def test_config_parses_embedder(tmp_path):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"knowledge": {"embedder": "local"}}))
    assert LangGraphConfig.from_yaml(str(p)).knowledge_embedder == "local"


def test_apply_embedder_builds_hybrid(tmp_path):
    from knowledge.hybrid_store import HybridKnowledgeStore
    cfg = _cfg("")
    cfg.knowledge_embedder = "local"
    cfg.knowledge_db_path = str(tmp_path / "kb.db")
    plugins = SimpleNamespace(knowledge_stores={}, embedders={"local": _embed_factory})
    out = _apply_plugin_knowledge_backend(cfg, object(), plugins)
    assert isinstance(out, HybridKnowledgeStore)


def test_apply_embedder_unregistered_or_error_keeps_builtin(tmp_path):
    builtin = object()
    cfg = _cfg("")
    cfg.knowledge_db_path = str(tmp_path / "kb.db")
    cfg.knowledge_embedder = "nope"
    assert _apply_plugin_knowledge_backend(cfg, builtin, SimpleNamespace(knowledge_stores={}, embedders={})) is builtin
    def _boom(config): raise RuntimeError("no model")
    cfg.knowledge_embedder = "b"
    assert _apply_plugin_knowledge_backend(cfg, builtin, SimpleNamespace(knowledge_stores={}, embedders={"b": _boom})) is builtin


def test_backend_takes_precedence_over_embedder(tmp_path):
    fake = _FakeBackend()
    cfg = _cfg("pg")
    cfg.knowledge_embedder = "local"
    cfg.knowledge_db_path = str(tmp_path / "kb.db")
    plugins = SimpleNamespace(knowledge_stores={"pg": lambda c: fake}, embedders={"local": _embed_factory})
    assert _apply_plugin_knowledge_backend(cfg, object(), plugins) is fake
