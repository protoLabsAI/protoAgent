"""Artifact plugin (ADR 0038) — the show_artifact tool + current store."""
import importlib.util
from pathlib import Path


def _load():
    spec = importlib.util.spec_from_file_location("artifact_plugin_under_test", Path("plugins/artifact/__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_show_artifact_stores_and_validates() -> None:
    art = _load()
    # Unknown kind is rejected, nothing stored.
    msg = art.show_artifact.invoke({"kind": "bogus", "code": "x"})
    assert "Unknown artifact kind" in msg
    # A valid kind stores the current artifact.
    art.show_artifact.invoke({"kind": "mermaid", "code": "graph TD; A-->B", "title": "Flow"})
    assert art._current["kind"] == "mermaid"
    assert art._current["code"] == "graph TD; A-->B"
    assert art._current["title"] == "Flow"
    assert art._current["ts"] > 0
