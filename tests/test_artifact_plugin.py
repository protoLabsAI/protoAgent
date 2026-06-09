"""Artifact plugin (ADR 0038) — show_artifact + the file-backed (cross-process) store."""
import importlib.util
from pathlib import Path


def _load():
    spec = importlib.util.spec_from_file_location("artifact_plugin_under_test", Path("plugins/artifact/__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_show_artifact_validates_and_persists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    art = _load()

    # Unknown kind rejected, nothing written.
    assert "Unknown artifact kind" in art.show_artifact.invoke({"kind": "bogus", "code": "x"})
    assert art._read_current()["ts"] == 0

    # Valid kind persists to disk (so a different process — the route — can read it).
    art.show_artifact.invoke({"kind": "mermaid", "code": "graph TD; A-->B", "title": "Flow"})
    cur = art._read_current()
    assert cur["kind"] == "mermaid" and cur["code"] == "graph TD; A-->B" and cur["title"] == "Flow"
    assert cur["ts"] > 0
    assert (tmp_path / "current.json").exists()
