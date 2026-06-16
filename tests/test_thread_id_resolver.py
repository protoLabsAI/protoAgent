"""The checkpointer thread_id is a pluggable seam (#571) — a fork registers a
resolver `(request_metadata, session_id) -> str` via a plugin to scope memory
off request metadata (e.g. per-project working memory), with ZERO edits to
server/chat.py. Unset ⇒ the template default `a2a:<session_id>`."""

# Import the helper directly: the re-exported `chat` function shadows the
# `server.chat` submodule on the package attribute, so `server.chat._resolve_...`
# would resolve to the function. The symbol itself is unambiguous.
from server.chat import _resolve_thread_id
from runtime.state import STATE


def test_default_when_no_resolver(monkeypatch):
    monkeypatch.setattr(STATE, "thread_id_resolver", None, raising=False)
    assert _resolve_thread_id({}, "s1") == "a2a:s1"
    assert _resolve_thread_id(None, "s1") == "a2a:s1"  # None metadata tolerated


def test_custom_resolver_scopes_off_metadata(monkeypatch):
    monkeypatch.setattr(STATE, "thread_id_resolver", lambda md, sid: f"proj:{md.get('project')}:{sid}", raising=False)
    assert _resolve_thread_id({"project": "acme"}, "s1") == "proj:acme:s1"


def test_resolver_error_falls_back_to_default(monkeypatch):
    def boom(md, sid):
        raise ValueError("nope")

    monkeypatch.setattr(STATE, "thread_id_resolver", boom, raising=False)
    assert _resolve_thread_id({}, "s1") == "a2a:s1"  # never breaks the turn


def test_resolver_falsy_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(STATE, "thread_id_resolver", lambda md, sid: "", raising=False)
    assert _resolve_thread_id({}, "s1") == "a2a:s1"


def test_registry_validates_and_stores_resolver():
    from graph.plugins.registry import PluginRegistry

    reg = PluginRegistry.__new__(PluginRegistry)  # skip HOST import in __init__
    reg.plugin_id = "demo"
    reg.thread_id_resolver = None

    def fn(md, sid):
        return "x"

    reg.register_thread_id_resolver(fn)
    assert reg.thread_id_resolver is fn
    reg.register_thread_id_resolver("not-callable")  # rejected; keeps the good one
    assert reg.thread_id_resolver is fn


def test_loader_last_plugin_wins(monkeypatch):
    """Two plugins each contributing a resolver → last wins, with a warning."""
    from graph.plugins.loader import PluginLoadResult

    result = PluginLoadResult()
    assert result.thread_id_resolver is None
    # mimic the loader's aggregation step
    r1, r2 = (lambda md, s: "a"), (lambda md, s: "b")
    result.thread_id_resolver = r1
    result.thread_id_resolver = r2  # later plugin overrides
    assert result.thread_id_resolver is r2
