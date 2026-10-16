"""The ``artifact.save_file`` plugin service (#4253): a plugin's background job registers a FILE
artifact without an agent turn — contained to its plugin store (or roots it names), under the
same blob cap / preview / version chain as ``save_file_artifact``."""

from __future__ import annotations

import os

import pytest

from tests.test_artifact_plugin import _arts, _load

pytestmark = pytest.mark.platform_sensitive  # symlinks + resolve()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A fresh artifact package plus a fake plugin store for plugin ``campaign``."""
    from graph import sdk

    art = _load(monkeypatch, tmp_path / "artifacts")
    stores = tmp_path / "stores"

    def fake_store(subdir: str = "", *, plugin_id: str):
        if not plugin_id or "/" in plugin_id:
            raise ValueError(f"plugin_id must be a bare plugin id, got {plugin_id!r}")
        d = stores / plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(sdk, "plugin_store", fake_store)
    return art, fake_store("campaign", plugin_id="campaign"), tmp_path


def test_saves_a_file_from_the_plugin_store_as_a_file_artifact(env):
    art, store, _ = env
    mp4 = store / "jobs" / "vid-1" / "final.mp4"
    mp4.parent.mkdir(parents=True)
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 2048)
    r = art.save_file_service(str(mp4), title="Overview", session_id="sess-1", plugin_id="campaign")
    assert r["ok"] is True and r["version"] == 1, r
    a = _arts(art)[0]
    assert a["id"] == r["id"] and a["kind"] == "file" and a["title"] == "Overview"
    meta = a["versions"][-1]["file"]
    assert meta["mime"] == "video/mp4" and meta["filename"] == "final.mp4" and meta["size"] == mp4.stat().st_size
    assert art._blob_path(a["id"], a["versions"][-1]["blob"]).read_bytes() == mp4.read_bytes()
    assert "Saved file artifact" in r["message"]
    # The chip tail rides separately (a caller with a live turn appends it last).
    assert r["ref"] and r["id"] in r["ref"] and r["ref"] not in r["message"]


def test_a_relative_path_is_taken_against_the_plugin_store_and_artifact_id_revises(env):
    art, store, _ = env
    (store / "sheet.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    first = art.save_file_service("sheet.png", plugin_id="campaign")
    assert first["ok"], first
    second = art.save_file_service("sheet.png", artifact_id=first["id"], plugin_id="campaign")
    assert second["ok"] and second["id"] == first["id"] and second["version"] == 2
    assert len(_arts(art)) == 1


@pytest.mark.parametrize("how", ["absolute", "dotdot", "symlink"])
def test_a_file_outside_the_store_is_refused(env, how):
    art, store, tmp = env
    secret = tmp / "secret.txt"
    secret.write_text("api_key=nope")
    if how == "absolute":
        path = str(secret)
    elif how == "dotdot":
        path = "../../secret.txt"
    else:
        (store / "link.txt").symlink_to(secret)
        path = str(store / "link.txt")
    r = art.save_file_service(path, plugin_id="campaign")
    assert r == {"ok": False, "id": "", "version": 0, "message": r["message"], "ref": ""}
    assert "outside this plugin's store" in r["message"]
    assert _arts(art) == []


def test_another_plugins_store_is_not_this_plugins(env):
    art, store, _ = env
    (store / "final.mp4").write_bytes(b"x")
    r = art.save_file_service(str(store / "final.mp4"), plugin_id="other")
    assert r["ok"] is False and "outside" in r["message"]


def test_an_explicit_root_is_allowed_but_never_a_broad_one(env):
    art, _, tmp = env
    data = tmp / "campaign-data"
    data.mkdir()
    (data / "cut.mp4").write_bytes(b"x" * 10)
    r = art.save_file_service(str(data / "cut.mp4"), plugin_id="campaign", roots=[str(data)])
    assert r["ok"], r
    for broad in (os.path.abspath(os.sep), os.path.expanduser("~")):
        r = art.save_file_service(str(data / "cut.mp4"), plugin_id="campaign", roots=[broad])
        assert r["ok"] is False and "too broad" in r["message"]
    r = art.save_file_service(str(data / "cut.mp4"), plugin_id="campaign", roots=["relative/dir"])
    assert r["ok"] is False and "absolute" in r["message"]


def test_plugin_id_is_required(env):
    art, store, _ = env
    (store / "a.txt").write_text("hi")
    r = art.save_file_service(str(store / "a.txt"), plugin_id="")
    assert r["ok"] is False and "plugin_id is required" in r["message"]
    with pytest.raises(TypeError):
        art.save_file_service(str(store / "a.txt"))  # keyword-only and required


def test_oversize_is_refused_as_data_naming_the_setting(env, monkeypatch):
    art, store, _ = env
    monkeypatch.setenv("ARTIFACT_MAX_BLOB_KB", "1")
    (store / "big.mp4").write_bytes(b"x" * 4096)
    r = art.save_file_service("big.mp4", plugin_id="campaign")
    assert r["ok"] is False and "max_blob_kb" in r["message"] and "ARTIFACT_MAX_BLOB_KB" in r["message"]
    assert _arts(art) == []


def test_missing_file_and_wrong_kind_revision_are_refusals(env):
    art, store, _ = env
    assert "No file at" in art.save_file_service("nope.mp4", plugin_id="campaign")["message"]
    html = art.show_service(kind="html", code="<p>x</p>")
    (store / "a.txt").write_text("hi")
    r = art.save_file_service("a.txt", artifact_id=html["id"], plugin_id="campaign")
    assert r["ok"] is False and "not a file" in r["message"]


def test_a_store_lock_timeout_is_a_refusal_not_a_raise(env, monkeypatch):
    art, store, _ = env
    (store / "a.txt").write_text("hi")
    tools = __import__(art.__name__ + "._tools", fromlist=["_tools"])

    def wedged(**_kw):
        raise tools._store.StoreLockTimeout("The artifact store is busy — nothing changed.")

    monkeypatch.setattr(tools, "_save_file", wedged)
    r = art.save_file_service("a.txt", plugin_id="campaign")
    assert r["ok"] is False and "busy" in r["message"]


def test_register_offers_save_file_by_name(env):
    from graph.plugins.testkit import FakeRegistry

    art, _, _ = env
    reg = FakeRegistry(plugin_id="artifact")
    art.register(reg)
    assert reg.services["artifact.save_file"] is art.save_file_service
    assert "plugin_store" in reg.service_meta["artifact.save_file"]["description"]


def test_the_tool_still_saves_and_refuses_oversize_before_reading(env, monkeypatch):
    art, store, _ = env
    p = store / "r.txt"
    p.write_text("hello")
    out = art.save_file_artifact.invoke({"path": str(p)})
    assert out.startswith("Saved file artifact") and "→ v1" in out
    monkeypatch.setenv("ARTIFACT_MAX_BLOB_KB", "1")
    p.write_bytes(b"x" * 4096)
    assert "File too large" in art.save_file_artifact.invoke({"path": str(p)})
