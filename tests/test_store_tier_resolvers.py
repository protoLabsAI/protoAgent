"""Store-tier resolution (ADR 0004 capstone) — every per-instance store lands under
``instance_root`` by default, and an explicit operator override is honored verbatim.

Pins the contract the scope_leaf→instance_root cutover established: there is no
per-call scoping knob any more, the instance root IS the scope, and a2a / telemetry /
checkpoints / skills are FILES directly under it while the rest are dirs.
"""

from __future__ import annotations

import pytest

import infra.paths as paths


@pytest.fixture
def box(monkeypatch, tmp_path):
    """Pin box_root to a tmp dir, scope to instance ``alice``, re-resolve."""
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path / "box"))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    for v in ("PROTOAGENT_HOME", "MEMORY_PATH", "GOAL_PATH", "TASKS_DB_PATH", "BEADS_DB_PATH", "KNOWLEDGE_DB_PATH"):
        monkeypatch.delenv(v, raising=False)
    paths.reset_instance_paths()
    return tmp_path / "box" / "alice"  # the instance root for alice


def test_memory_default_and_override(box, monkeypatch):
    from graph.middleware.memory import memory_path

    assert memory_path() == str(box / "memory")
    monkeypatch.setenv("MEMORY_PATH", "/custom/mem")
    assert memory_path() == "/custom/mem"  # env override verbatim


def test_tasks_default_and_override(box, monkeypatch):
    from tasks.store import _resolve_db_path

    assert _resolve_db_path(None) == box / "tasks" / "issues.db"
    monkeypatch.setenv("TASKS_DB_PATH", "/custom/tasks.db")
    assert _resolve_db_path(None) == __import__("pathlib").Path("/custom/tasks.db")
    monkeypatch.delenv("TASKS_DB_PATH")
    monkeypatch.setenv("BEADS_DB_PATH", "/legacy/beads.db")  # legacy alias still honored
    assert _resolve_db_path(None) == __import__("pathlib").Path("/legacy/beads.db")


def test_a2a_stores_are_files_under_instance_root(box):
    from a2a_impl.stores import _resolve_db_path

    assert _resolve_db_path("a2a-tasks.db") == str(box / "a2a-tasks.db")
    assert _resolve_db_path("a2a-push.db") == str(box / "a2a-push.db")


def test_acp_session_id_path_under_instance_store(box):
    from plugins.coding_agent import _session_id_path

    spec = {
        "name": "coder",
        "command": "x",
        "args": (),
        "workdir": "/",
        "permissions": "readonly",
        "allow_kinds": (),
        "deny_kinds": (),
    }
    p = _session_id_path(spec)
    assert p.parent == box / "acp_sessions" and p.suffix == ".json"


def test_checkpoint_and_skills_are_files_at_instance_root(box):
    from server.agent_init import _resolve_checkpoint_db, _resolve_skills_db

    assert _resolve_checkpoint_db("/sandbox/checkpoints.db") == str(box / "checkpoints.db")
    assert _resolve_skills_db("/sandbox/skills.db", shared=False) == str(box / "skills.db")


def test_two_instances_get_disjoint_store_roots(box, monkeypatch, tmp_path):
    from tasks.store import _resolve_db_path

    a = _resolve_db_path(None)
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "bob")
    paths.reset_instance_paths()
    b = _resolve_db_path(None)
    assert a != b
    assert b == tmp_path / "box" / "bob" / "tasks" / "issues.db"
