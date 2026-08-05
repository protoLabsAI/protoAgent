"""A rename must not orphan an agent's per-agent stores (#2382).

`inbox` / `background` / `activity` were keyed by `agent_name()` — the EDITABLE display
name — so renaming an agent silently pointed it at a brand-new empty database. Nothing was
deleted; both files sit side by side on disk (a real fleet member ended up with
`inbox/traderAgent.db` next to `inbox/merchantBot.db`) and the agent simply stopped looking
at the old one. The name was never the scope: `instance_paths().store(...)` is already
private to this instance, so the filename is a constant now.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    """Point the per-instance store root at a temp dir and pin a display name."""
    import server.agent_init as ai

    monkeypatch.setattr(ai, "instance_paths", lambda: type("P", (), {"store": lambda _s, n: tmp_path / n})())
    monkeypatch.setattr(ai, "agent_name", lambda: "traderAgent")
    return tmp_path


def test_store_path_is_constant_so_a_rename_cannot_move_it(store_root, monkeypatch):
    import server.agent_init as ai

    first = ai._agent_store_db("inbox")
    assert first == store_root / "inbox" / "agent.db"
    first.write_bytes(b"")  # the store exists now

    # THE regression: rename the agent and resolve again — same file, not a fresh one.
    monkeypatch.setattr(ai, "agent_name", lambda: "merchantBot")
    assert ai._agent_store_db("inbox") == first


def test_a_lone_name_keyed_store_is_adopted_in_place(store_root):
    """Existing installs keep their data: the private dir can only hold THIS agent's store,
    so a single name-keyed file is it, whatever it's called. Used as-is — no move, no copy,
    nothing that could be interrupted half-way."""
    import server.agent_init as ai

    legacy = store_root / "background"
    legacy.mkdir()
    (legacy / "traderAgent.db").write_bytes(b"old-data")

    assert ai._agent_store_db("background") == legacy / "traderAgent.db"
    assert (legacy / "traderAgent.db").read_bytes() == b"old-data"  # untouched
    assert not (legacy / "agent.db").exists()  # and no empty sibling conjured next to it


def test_ambiguous_leftovers_start_clean_and_say_so(store_root, caplog):
    """A workspace renamed BEFORE this fix carries two name-keyed stores and nothing on disk
    says which is current — guessing would silently pick someone's stale history. Start clean
    at the constant path and name both files in the log instead."""
    import server.agent_init as ai

    d = store_root / "activity"
    d.mkdir()
    (d / "traderAgent.db").write_bytes(b"a")
    (d / "merchantBot.db").write_bytes(b"b")

    with caplog.at_level("WARNING"):
        assert ai._agent_store_db("activity") == d / "agent.db"
    assert "traderAgent.db" in caplog.text and "merchantBot.db" in caplog.text


def test_the_constant_path_wins_once_it_exists(store_root):
    """After adoption-by-nothing, a stale name-keyed file must never pull the agent back off
    its real store."""
    import server.agent_init as ai

    d = store_root / "inbox"
    d.mkdir()
    (d / "agent.db").write_bytes(b"current")
    (d / "traderAgent.db").write_bytes(b"stale")

    assert ai._agent_store_db("inbox") == d / "agent.db"


def test_a_configured_shared_dir_stays_namespaced_by_name(store_root, tmp_path):
    """`inbox_db_path` is a DIRECTORY several agents may deliberately share, so there the
    per-agent filename is load-bearing — a constant would have them all open one database."""
    import server.agent_init as ai

    shared = tmp_path / "shared-inbox"
    assert ai._agent_store_db("inbox", shared_dir=shared) == shared / "traderAgent.db"
    assert shared.is_dir()  # created for the caller, like the private path


def test_builders_resolve_through_the_shared_helper(store_root, monkeypatch):
    """All three stores go through `_agent_store_db` — a builder that kept its own
    name-keyed path would reintroduce the bug for that one store only."""
    import server.agent_init as ai

    seen: list[str] = []
    monkeypatch.setattr(ai, "_agent_store_db", lambda store, **kw: (seen.append(store), store_root / f"{store}.db")[1])
    monkeypatch.setattr(ai, "instance_paths", lambda: type("P", (), {"store": lambda _s, n: store_root / n})())

    cfg = type("C", (), {"inbox_db_path": "", "auth_token": ""})()
    ai._build_inbox_store(cfg)
    ai._build_activity_log(cfg)
    monkeypatch.setenv("BACKGROUND_DISABLED", "")
    ai._build_background_manager(cfg)

    assert seen == ["inbox", "activity", "background"]
