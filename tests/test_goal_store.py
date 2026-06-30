"""GoalStore round-trip + path fallback — goal mode."""

from graph.goals.store import GoalStore, _safe_name
from graph.goals.types import GoalState


def test_set_get_round_trip(tmp_path):
    store = GoalStore(tmp_path)
    state = GoalState(session_id="s1", condition="all tests pass", verifier={"type": "command", "command": "true"})
    store.set(state)
    loaded = store.get("s1")
    assert loaded is not None
    assert loaded.condition == "all tests pass"
    assert loaded.verifier["command"] == "true"
    assert loaded.active


def test_get_missing_is_none(tmp_path):
    assert GoalStore(tmp_path).get("nope") is None


def test_clear(tmp_path):
    store = GoalStore(tmp_path)
    store.set(GoalState(session_id="s2", condition="x"))
    assert store.clear("s2") is True
    assert store.get("s2") is None
    assert store.clear("s2") is False


def test_session_id_is_sanitized(tmp_path):
    store = GoalStore(tmp_path)
    store.set(GoalState(session_id="a/b:c", condition="x"))
    # written under a filesystem-safe name, still retrievable by raw id
    assert store.get("a/b:c") is not None
    assert "/" not in _safe_name("a/b:c")


def test_forward_compatible_unknown_keys(tmp_path):
    store = GoalStore(tmp_path)
    p = store._path("s3")
    p.write_text('{"session_id": "s3", "condition": "x", "future_key": 1}')
    loaded = store.get("s3")
    assert loaded is not None and loaded.condition == "x"


def test_all_lists_every_session(tmp_path):
    store = GoalStore(tmp_path)
    store.set(GoalState(session_id="s1", condition="first"))
    store.set(GoalState(session_id="s2", condition="second"))
    store.set(GoalState(session_id="s3", condition="third"))

    states = store.all()
    assert {s.session_id for s in states} == {"s1", "s2", "s3"}
    assert {s.condition for s in states} == {"first", "second", "third"}


def test_all_empty_and_skips_corrupt(tmp_path):
    store = GoalStore(tmp_path)
    assert store.all() == []
    store.set(GoalState(session_id="ok", condition="c"))
    (tmp_path / "broken.json").write_text("{ not json")  # must be skipped, not raise
    states = store.all()
    assert [s.session_id for s in states] == ["ok"]


def test_resolve_base_is_instance_scoped(monkeypatch, tmp_path):
    """Two agents on one machine must not share a goals dir (ADR 0004) — the default
    sits at instance_root/goals, so a different PROTOAGENT_INSTANCE → a different dir."""
    import infra.paths as paths
    from graph.goals import store as goal_store

    monkeypatch.delenv("GOAL_PATH", raising=False)
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))

    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alpha")
    paths.reset_instance_paths()
    base_a = goal_store._resolve_base()
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "beta")
    paths.reset_instance_paths()
    base_b = goal_store._resolve_base()
    assert base_a != base_b
    assert base_a == tmp_path / "alpha" / "goals"
    assert base_b == tmp_path / "beta" / "goals"

    # GOAL_PATH is an explicit override — used verbatim, no scoping.
    monkeypatch.setenv("GOAL_PATH", str(tmp_path / "explicit"))
    paths.reset_instance_paths()
    assert goal_store._resolve_base() == tmp_path / "explicit"


def test_set_and_clear_publish_goal_changed(tmp_path, monkeypatch):
    """A goal write/clear pushes `goal.changed` on the bus so the console Goals panel
    invalidates live instead of polling every 5s (#1310)."""
    from graph.plugins import host

    events: list[tuple] = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    store = GoalStore(tmp_path)
    store.set(GoalState(session_id="s1", condition="x"))
    assert store.clear("s1") is True
    assert [t for t, _ in events] == ["goal.changed", "goal.changed"]
    assert events[0][1]["session_id"] == "s1"


def test_clear_missing_does_not_publish(tmp_path, monkeypatch):
    from graph.plugins import host

    events: list[tuple] = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    assert GoalStore(tmp_path).clear("nope") is False
    assert events == []  # nothing was cleared → no event
