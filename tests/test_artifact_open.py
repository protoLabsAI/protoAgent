"""File opening uses a version-bound media grant, never an operator bearer."""
import re
from urllib.parse import urlsplit

import pytest

from tests.test_artifact_media import _client, _save
from tests.test_artifact_plugin import _load

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def artifact(monkeypatch, tmp_path):
    from infra import media

    monkeypatch.setattr(media, "_signing_key", lambda: "test-artifact-open-key")
    monkeypatch.setattr(media, "public_base_url", lambda: "http://testserver")
    return _load(monkeypatch, tmp_path)


def _url(reply):
    return re.search(r"\]\(([^)]+)\)", reply)[1]


def test_open_and_fallback(artifact, tmp_path, monkeypatch):
    aid = _save(artifact, tmp_path, "report.txt", b"first")
    events = []
    monkeypatch.setattr(artifact._store, "_emit", lambda topic, data: events.append((topic, data)))
    result = artifact.open_artifact.invoke({"artifact_id": aid, "state": {"session_id": "chat"}})
    url = _url(result)
    assert events == [("open_url", {"session_id": "chat", "path": url.removeprefix("http://testserver")})]
    assert "token=" not in url and "bearer" not in url
    response = _client(artifact).get(url)
    assert response.status_code == 200 and response.content == b"first"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["content-disposition"].startswith("inline")
    events.clear()
    result = artifact.open_artifact.invoke({"artifact_id": aid, "state": {"renders_ui": False}})
    assert "No console attached" in result and _url(result).startswith("http://testserver/")
    assert not events


def test_version_and_ticket_scope(artifact, tmp_path):
    aid = _save(artifact, tmp_path, "report.txt", b"first")
    _save(artifact, tmp_path, "report.txt", b"second", aid)
    client = _client(artifact)
    old = _url(artifact.open_artifact.invoke({"artifact_id": aid, "version": 1}))
    latest = _url(artifact.open_artifact.invoke({"artifact_id": aid}))
    assert client.get(old).content == b"first"
    assert client.get(latest).content == b"second"
    assert client.get(old.replace("version=1", "version=2")).status_code == 404
    assert client.get(urlsplit(old).path).status_code == 404
    assert client.get(re.sub(r"t=[a-f0-9]+$", "t=é", old)).status_code == 404
    assert client.get(re.sub(r"exp=\d+", "exp=1", old)).status_code == 404
    assert client.get(old[:-1] + ("0" if old[-1] != "0" else "1")).status_code == 404
    store = artifact._read_store()
    store["artifacts"][0]["versions"].pop(0)
    artifact._write_store(store)
    assert client.get(old).status_code == 404


@pytest.mark.parametrize("version", [-1, 9])
def test_bad_version(artifact, tmp_path, version):
    aid = _save(artifact, tmp_path, "report.txt", b"first")
    assert "no version" in artifact.open_artifact.invoke({"artifact_id": aid, "version": version})


def test_unknown_and_non_file(artifact):
    assert "unknown artifact" in artifact.open_artifact.invoke({"artifact_id": "missing"})
    artifact.show_artifact.invoke({"kind": "html", "code": "hello"})
    aid = artifact._read_store()["artifacts"][0]["id"]
    assert "not a file artifact" in artifact.open_artifact.invoke({"artifact_id": aid})


def test_linked_file_rechecks_live_fence(artifact, tmp_path, monkeypatch):
    from tests.test_artifact_linked import _fence

    _fence(monkeypatch, artifact, workspace=tmp_path)
    source = tmp_path / "linked.txt"
    source.write_text("linked bytes")
    artifact.save_file_artifact.invoke({"path": "linked.txt", "project": "workspace", "link": True})
    aid = artifact._read_store()["artifacts"][0]["id"]
    url = _url(artifact.open_artifact.invoke({"artifact_id": aid}))
    client = _client(artifact)
    assert client.get(url).content == b"linked bytes"
    _fence(monkeypatch, artifact)
    assert client.get(url).status_code == 404


def test_missing_file_is_readable_error(artifact, tmp_path):
    aid = _save(artifact, tmp_path, "report.txt", b"first")
    value = artifact._read_store()["artifacts"][0]["versions"][0]
    artifact._blob_path(aid, value["blob"]).unlink()
    assert "blob missing" in artifact.open_artifact.invoke({"artifact_id": aid})
