"""The checkpointer thread_id is a pluggable seam (#571) — a fork registers a
resolver `(request_metadata, session_id) -> str` via a plugin to scope memory
off request metadata (e.g. per-project working memory), with ZERO edits to
server/chat.py. Unset ⇒ the template default `a2a:<session_id>`."""

# Import the helper directly: the re-exported `chat` function shadows the
# `server.chat` submodule on the package attribute, so `server.chat._resolve_...`
# would resolve to the function. The symbol itself is unambiguous.
from types import SimpleNamespace

from server.chat import _goal_continuation_config, _resolve_thread_id
from runtime.state import STATE


def _goal(fresh, iteration=3):
    return SimpleNamespace(fresh_context=fresh, iteration=iteration)


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


# --- _goal_continuation_config (unify the drive-loop fresh-context thread-id) ---------


def test_same_session_goal_reuses_config():
    # Non-fresh-context: the checkpointer keeps history, so continuation reuses the config
    # object as-is (identity — no new dict).
    cfg = {"configurable": {"thread_id": "a2a:s1"}, "recursion_limit": 200}
    assert _goal_continuation_config(cfg, _goal(False)) is cfg
    assert _goal_continuation_config(cfg, None) is cfg  # no active goal state


def test_fresh_context_scopes_thread_from_current_config():
    # Streaming (a2a:) and non-streaming (chat:) bases both derive the SAME shape — the
    # per-iteration sub-thread comes from the CURRENT thread_id (no drift), recursion_limit
    # normalized on both.
    a2a = _goal_continuation_config({"configurable": {"thread_id": "a2a:s1"}, "recursion_limit": 200}, _goal(True, 4))
    assert a2a == {"configurable": {"thread_id": "a2a:s1:goal-iter-4"}, "recursion_limit": 200}
    chat = _goal_continuation_config({"configurable": {"thread_id": "chat:s1"}}, _goal(True, 1))
    assert chat == {"configurable": {"thread_id": "chat:s1:goal-iter-1"}, "recursion_limit": 200}


def test_fresh_context_missing_thread_id_falls_back():
    out = _goal_continuation_config({}, _goal(True, 2))
    assert out["configurable"]["thread_id"] == "goal:goal-iter-2"
