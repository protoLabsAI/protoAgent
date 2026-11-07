"""ADR 0125 P2-G1 (D7): the shared operator profile store + its config/settings wiring.

Covers:
  - SharedProfileStore accepts only kind in (profile, standing); agent writes land
    review_state='pending', operator writes 'confirmed'.
  - memory.shared_profile defaults False and round-trips through config load/save.
  - Disabled (the default) => STATE builder returns None and profile.db is never opened.
  - The Settings schema exposes the toggle under a "Memory" section.
"""

import textwrap
from pathlib import Path

import pytest

from graph.config import LangGraphConfig
from graph.config_io import apply_updates_to_yaml, config_to_dict, save_yaml_doc
from knowledge.shared_profile import ALLOWED_KINDS, SharedProfileStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    # The private KNOWLEDGE_DB_PATH override must NOT redirect the un-scoped commons
    # profile.db; clear it so the test asserts the verbatim commons path either way.
    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    return SharedProfileStore(tmp_path)


# ── r1: kind restriction + provenance-driven review_state ────────────────────


def test_opens_profile_db_under_the_commons_root(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    s = SharedProfileStore(tmp_path)
    assert s.path == tmp_path / "profile.db"
    assert s.path.exists()  # schema created on open


def test_accepts_profile_and_standing(store):
    pid = store.add("Josh — America/Chicago", kind="profile", provenance="operator")
    sid = store.add("prefers terse replies", kind="standing", provenance="operator")
    assert store.get(pid).kind == "profile"
    assert store.get(sid).kind == "standing"
    assert ALLOWED_KINDS == {"profile", "standing"}


@pytest.mark.parametrize("bad", ["fact", "decision"])
def test_rejects_non_profile_kinds(store, bad):
    with pytest.raises(ValueError):
        store.add("not allowed here", kind=bad, provenance="operator")
    # Rejected before any row lands.
    assert store.count() == 0


def test_agent_writes_pending_operator_writes_confirmed(store):
    agent_id = store.add("timezone guess", kind="standing", provenance="agent")
    op_id = store.add("timezone confirmed", kind="standing", provenance="operator")
    assert store.get(agent_id).review_state == "pending"
    assert store.get(op_id).review_state == "confirmed"


# ── r2: config default + round-trip ──────────────────────────────────────────


def _write_cfg(dir_path: Path, body: str) -> str:
    path = dir_path / "langgraph-config.yaml"
    path.write_text(textwrap.dedent(body))
    return str(path)


def test_shared_profile_defaults_false():
    assert LangGraphConfig().memory_shared_profile is False


def test_shared_profile_parsed_from_yaml(tmp_path):
    path = _write_cfg(tmp_path, "memory:\n  shared_profile: true\n")
    assert LangGraphConfig.from_yaml(path).memory_shared_profile is True


def test_shared_profile_round_trips_through_config_io(tmp_path):
    cfg = LangGraphConfig()
    cfg.memory_shared_profile = True
    doc: dict = {}
    apply_updates_to_yaml(doc, config_to_dict(cfg))
    # config_to_dict is FIELDS-driven: the toggle serializes under the memory section.
    assert doc["memory"]["shared_profile"] is True
    out = tmp_path / "langgraph-config.yaml"
    save_yaml_doc(doc, out)
    assert LangGraphConfig.from_yaml(str(out)).memory_shared_profile is True


# ── r3: disabled => no store, profile.db never opened ────────────────────────


def test_disabled_builds_no_store_and_never_opens_profile_db(tmp_path, monkeypatch):
    from server.agent_init import _build_shared_profile_store

    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    commons = tmp_path / "commons"
    cfg = LangGraphConfig()  # memory_shared_profile defaults False
    cfg.commons_path = str(commons)
    assert cfg.memory_shared_profile is False

    assert _build_shared_profile_store(cfg) is None
    assert not (commons / "profile.db").exists()


def test_enabled_builds_store_and_opens_profile_db(tmp_path, monkeypatch):
    from server.agent_init import _build_shared_profile_store

    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    commons = tmp_path / "commons"
    cfg = LangGraphConfig()
    cfg.memory_shared_profile = True
    cfg.commons_path = str(commons)

    built = _build_shared_profile_store(cfg)
    assert isinstance(built, SharedProfileStore)
    assert built.path == commons / "profile.db"
    assert (commons / "profile.db").exists()


# ── r4: Settings schema exposes the toggle under "Memory" ────────────────────


def test_settings_schema_exposes_memory_toggle():
    from graph.settings_schema import _SECTION_CATEGORY, build_schema

    groups = build_schema(LangGraphConfig())
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    field = by_key["memory.shared_profile"]
    assert field["type"] == "bool"
    assert field["section"] == "Memory"
    assert field["default"] is False
    # The "Memory" section is a mapped core section, so it renders under a category.
    section_cat = {g["section"]: g["category"] for g in groups}
    assert section_cat["Memory"] == _SECTION_CATEGORY["Memory"]
