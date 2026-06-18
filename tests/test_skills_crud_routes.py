"""Skills CRUD routes (``/api/playbooks`` POST/GET-one/PUT, file-aware DELETE).

Operator-authored skills are persisted as real ``SKILL.md`` files under the
user-skills root and indexed live, so these run end-to-end against a real
``SkillsIndex`` on a tmp DB with the user root redirected into ``tmp_path`` — the
write path (file + index) and the editable/read-only classification are what we
care about, and a real index is cheaper than faking both halves.
"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import operator_api.knowledge_routes as kr
from graph.skills.index import SkillsIndex
from operator_api.knowledge_routes import register_knowledge_routes


def _client(monkeypatch, tmp_path):
    idx = SkillsIndex(db_path=str(tmp_path / "skills.db"))
    root = tmp_path / "userskills"
    monkeypatch.setattr(kr, "_user_skills_root", lambda: root)
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "skills_index", idx, raising=False)
    monkeypatch.setattr(rs.STATE, "knowledge_store", None, raising=False)
    app = FastAPI()
    register_knowledge_routes(app)
    return TestClient(app), idx, root


def _seed_emitted(idx, name="Learned thing", desc="learned desc", body="learned body"):
    idx.add_skill(
        SimpleNamespace(name=name, description=desc, prompt_template=body, tools_used=[], source_session_id=""),
        source="emitted",
    )


def test_create_writes_skill_md_and_indexes(monkeypatch, tmp_path):
    c, idx, root = _client(monkeypatch, tmp_path)
    r = c.post(
        "/api/playbooks",
        json={
            "name": "Release Notes",
            "description": "Draft release notes from merged PRs",
            "prompt_template": "1. gather PRs\n2. group by area",
            "tools_used": ["github", "git"],
            "user_facing": True,
            "slash": "relnotes",
        },
    )
    assert r.status_code == 200
    skill = r.json()["skill"]
    assert skill["origin"] == "user" and skill["editable"] is True
    # The file landed under the user root in the portable SKILL.md format.
    md = (root / "release-notes" / "SKILL.md").read_text()
    assert "name: Release Notes" in md and "1. gather PRs" in md
    # …and it's live in the index (source=disk, no restart needed).
    rows = idx.all_skills()
    assert [s["name"] for s in rows] == ["Release Notes"] and rows[0]["source"] == "disk"
    # GET one carries the full body the list omits.
    detail = c.get(f"/api/playbooks/{skill['id']}").json()["skill"]
    assert detail["prompt_template"].startswith("1. gather PRs")


def test_create_requires_fields(monkeypatch, tmp_path):
    c, _idx, _root = _client(monkeypatch, tmp_path)
    assert c.post("/api/playbooks", json={"name": "x", "description": "y"}).status_code == 400


def test_create_rejects_duplicate_name(monkeypatch, tmp_path):
    c, _idx, _root = _client(monkeypatch, tmp_path)
    base = {"name": "Dupe", "description": "d", "prompt_template": "b"}
    assert c.post("/api/playbooks", json=base).status_code == 200
    assert c.post("/api/playbooks", json={**base, "description": "d2"}).status_code == 409


def test_edit_rewrites_file_and_reindexes(monkeypatch, tmp_path):
    c, idx, root = _client(monkeypatch, tmp_path)
    created = c.post("/api/playbooks", json={"name": "Edit Me", "description": "old", "prompt_template": "old body"}).json()
    sid = created["id"]
    r = c.put(f"/api/playbooks/{sid}", json={"name": "Edit Me", "description": "new desc", "prompt_template": "new body"})
    assert r.status_code == 200
    md = (root / "edit-me" / "SKILL.md").read_text()
    assert "new desc" in md and "new body" in md and "old body" not in md
    names = [s["name"] for s in idx.all_skills()]
    assert names == ["Edit Me"]  # no duplicate row left behind


def test_edit_learned_materializes_durable_file(monkeypatch, tmp_path):
    c, idx, root = _client(monkeypatch, tmp_path)
    _seed_emitted(idx)
    sid = idx.all_skills()[0]["id"]
    # Before edit: a learned (DB-only) skill, no file.
    assert not (root / "learned-thing").exists()
    r = c.put(
        f"/api/playbooks/{sid}",
        json={"name": "Learned thing", "description": "curated", "prompt_template": "curated body"},
    )
    assert r.status_code == 200 and r.json()["skill"]["origin"] == "user"
    assert (root / "learned-thing" / "SKILL.md").is_file()  # now durable


def test_delete_user_skill_removes_file(monkeypatch, tmp_path):
    c, idx, root = _client(monkeypatch, tmp_path)
    sid = c.post("/api/playbooks", json={"name": "Trash Me", "description": "d", "prompt_template": "b"}).json()["id"]
    assert (root / "trash-me" / "SKILL.md").is_file()
    assert c.delete(f"/api/playbooks/{sid}").json() == {"enabled": True, "deleted": True}
    assert not (root / "trash-me").exists() and idx.all_skills() == []


def test_delete_bundled_is_blocked(monkeypatch, tmp_path):
    c, idx, root = _client(monkeypatch, tmp_path)
    # A disk skill with NO file under the user root == a bundled/plugin example.
    idx.add_skill(
        SimpleNamespace(name="Bundled", description="d", prompt_template="b", tools_used=[], source_session_id=""),
        source="disk",
    )
    sid = idx.all_skills()[0]["id"]
    body = c.delete(f"/api/playbooks/{sid}").json()
    assert body["deleted"] is False and "read-only" in body["error"]
    assert idx.all_skills()  # row survived


def test_list_tags_origin_and_editable(monkeypatch, tmp_path):
    c, idx, _root = _client(monkeypatch, tmp_path)
    c.post("/api/playbooks", json={"name": "Mine", "description": "d", "prompt_template": "b"})
    _seed_emitted(idx, name="Emitted one")
    idx.add_skill(
        SimpleNamespace(name="Shipped", description="d", prompt_template="b", tools_used=[], source_session_id=""),
        source="disk",
    )
    by_name = {p["name"]: p for p in c.get("/api/playbooks").json()["playbooks"]}
    assert by_name["Mine"]["origin"] == "user" and by_name["Mine"]["editable"] is True
    assert by_name["Emitted one"]["origin"] == "learned" and by_name["Emitted one"]["editable"] is True
    assert by_name["Shipped"]["origin"] == "bundled" and by_name["Shipped"]["editable"] is False
