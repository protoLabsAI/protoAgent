"""An archetype's persona must not commit to actions the agent has no tool for (#2277)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from graph.workspaces import manager


def _member(tmp_path, monkeypatch, record: dict | None):
    """Make THIS process look like a workspace member whose record is `record`."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    if record is not None:
        (ws / "workspace.yaml").write_text(yaml.safe_dump(record), encoding="utf-8")

    class _Paths:
        instance_root = ws

    monkeypatch.setattr("infra.paths.instance_paths", lambda: _Paths())
    return ws


def test_a_declared_tool_that_never_bound_is_reported(tmp_path, monkeypatch):
    """The shipped bug: the PM preset said pain points 'get filed as issues', but
    github.write defaults false so github_create_issue was never registered — and the
    agent narrated filings that had not happened."""
    _member(tmp_path, monkeypatch, {"id": "pm", "requires_tools": ["github_create_issue"]})

    warning = manager.capability_contract_warning(["read_file", "task"])

    assert warning and "github_create_issue" in warning
    assert "narrate" in warning  # names the silent failure mode, not just the gap


def test_a_satisfied_contract_is_silent(tmp_path, monkeypatch):
    _member(tmp_path, monkeypatch, {"id": "pm", "requires_tools": ["github_create_issue"]})

    assert manager.capability_contract_warning(["github_create_issue", "read_file"]) is None


def test_only_the_missing_tools_are_named(tmp_path, monkeypatch):
    _member(tmp_path, monkeypatch, {"id": "pm", "requires_tools": ["a", "b", "c"]})

    warning = manager.capability_contract_warning(["b"])

    assert "a" in warning and "c" in warning


def test_a_member_without_a_contract_is_silent(tmp_path, monkeypatch):
    _member(tmp_path, monkeypatch, {"id": "pm"})
    assert manager.capability_contract_warning([]) is None


def test_a_non_member_instance_is_silent(tmp_path, monkeypatch):
    """A hub or standalone instance root has no workspace.yaml at all."""
    _member(tmp_path, monkeypatch, None)
    assert manager.capability_contract_warning([]) is None


def test_an_empty_bound_set_still_reports_rather_than_crashing(tmp_path, monkeypatch):
    """Pre-setup the graph may not exist yet; the check must degrade, not raise."""
    _member(tmp_path, monkeypatch, {"id": "pm", "requires_tools": ["x"]})

    assert manager.capability_contract_warning(None)


# ── create() records the contract ─────────────────────────────────────────────
def test_create_writes_the_contract_onto_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))

    rec = manager.create("pm-agent", requires_tools=["github_create_issue"])

    stored = yaml.safe_load((Path(rec["path"]) / "workspace.yaml").read_text())
    assert stored["requires_tools"] == ["github_create_issue"]


def test_create_without_a_contract_omits_the_key(tmp_path, monkeypatch):
    """An archetype that commits to nothing tool-backed adds no noise to the record."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))

    rec = manager.create("plain-agent")

    stored = yaml.safe_load((Path(rec["path"]) / "workspace.yaml").read_text())
    assert "requires_tools" not in stored


# ── the shipped catalog ───────────────────────────────────────────────────────
def test_project_manager_archetype_declares_the_tool_its_doctrine_needs():
    """The archetype whose doctrine sent a PM chasing an unbindable tool now says so.

    The row is currently HELD from the picker (first-run friction, 2026-08-21 —
    see test_project_manager_archetype_is_held), but the contract must ride along
    in `held` so restoration brings it back intact."""
    catalog = json.loads((Path(__file__).resolve().parents[1] / "config" / "archetype-catalog.json").read_text())
    pm = next(a for a in catalog.get("held", []) if a["id"] == "project-manager")

    assert "github_create_issue" in pm.get("requires_tools", [])
