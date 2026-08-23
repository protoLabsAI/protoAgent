"""An archetype's persona must not commit to actions the agent has no tool for (#2277)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from graph.workspaces import manager


def _member(tmp_path, monkeypatch, record: dict | None):
    """Make THIS process look like a workspace member whose record is `record` — the
    way the hub's supervisor does it: ``PROTOAGENT_HOME=<ws>`` (ADR 0042/0065), so every
    instance path (the record, the config dir, the setup marker) resolves under it."""
    from infra.paths import reset_instance_paths

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    if record is not None:
        (ws / "workspace.yaml").write_text(yaml.safe_dump(record), encoding="utf-8")
    monkeypatch.setenv("PROTOAGENT_HOME", str(ws))
    reset_instance_paths()
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


# ── the host path: archetype-contract.yaml next to the setup marker ───────────
# The setup wizard installs an archetype onto the HOST, which has no workspace.yaml;
# POST /api/config/setup records the contract in <config_dir>/archetype-contract.yaml
# instead (graph.config_io.write_host_archetype) and the warning falls back to it.
def _host(tmp_path, monkeypatch):
    """THIS process is the host: an instance root with no workspace.yaml."""
    return _member(tmp_path, monkeypatch, None)


def test_a_wizard_installed_contract_is_checked_on_the_host(tmp_path, monkeypatch):
    """The gap #2980 documented: a wizard-created host Project Manager got no banner."""
    from graph.config_io import write_host_archetype

    _host(tmp_path, monkeypatch)
    write_host_archetype(["github_create_issue"])

    warning = manager.capability_contract_warning(["read_file", "task"])

    assert warning and "github_create_issue" in warning


def test_a_satisfied_host_contract_is_silent(tmp_path, monkeypatch):
    from graph.config_io import write_host_archetype

    _host(tmp_path, monkeypatch)
    write_host_archetype(["github_create_issue"])

    assert manager.capability_contract_warning(["github_create_issue"]) is None


def test_a_host_without_a_record_is_silent(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch)
    assert manager.capability_contract_warning([]) is None


def test_the_workspace_record_wins_over_a_host_record(tmp_path, monkeypatch):
    """A member with a contract-free workspace.yaml must not inherit a stray host
    record from the same config dir — the fallback is for NO workspace record only."""
    from graph.config_io import write_host_archetype

    _member(tmp_path, monkeypatch, {"id": "plain"})
    write_host_archetype(["github_create_issue"])

    assert manager.capability_contract_warning([]) is None


def test_write_host_archetype_round_trips_and_an_empty_contract_removes_the_file(tmp_path, monkeypatch):
    from graph.config_io import host_archetype_path, read_host_archetype, setup_marker_path, write_host_archetype

    ws = _host(tmp_path, monkeypatch)
    write_host_archetype(["a", "b"])
    assert read_host_archetype() == {"requires_tools": ["a", "b"]}
    # Sibling of the setup marker, under this instance's config dir — the same place the
    # wizard's other state lives.
    assert host_archetype_path() == ws / "config" / "archetype-contract.yaml"
    assert host_archetype_path().parent == setup_marker_path().parent
    write_host_archetype([])
    assert not host_archetype_path().exists()
    assert read_host_archetype() is None


def test_reset_setup_drops_the_contract_with_the_marker(tmp_path, monkeypatch):
    """Re-running the wizard (POST /api/config/reset-setup) must not carry the LAST
    run's contract into the next one — the re-run records its own."""
    from graph.config_io import host_archetype_path, mark_setup_complete, reset_setup, setup_marker_path, write_host_archetype

    _host(tmp_path, monkeypatch)
    mark_setup_complete()
    write_host_archetype(["github_create_issue"])
    reset_setup()
    assert not setup_marker_path().exists()
    assert not host_archetype_path().exists()
    reset_setup()  # idempotent, like the marker


def test_the_host_contract_file_is_gitignored():
    """Runtime state, never config to commit: in a fork (this is a template repo) a
    committed contract would put the banner on every clone. Both the default instance
    (config/) and a scoped one (config/<id>/) must be covered, like .setup-complete."""
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists():  # a sdist / wheel checkout has no gitignore to check
        import pytest

        pytest.skip("not a git checkout")
    for rel in ("config/archetype-contract.yaml", "config/dev/archetype-contract.yaml"):
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo)
        assert r.returncode == 0, f"{rel} is not gitignored"


def test_read_host_archetype_tolerates_garbage(tmp_path, monkeypatch):
    from graph.config_io import host_archetype_path, read_host_archetype

    _host(tmp_path, monkeypatch)
    host_archetype_path().parent.mkdir(parents=True, exist_ok=True)
    host_archetype_path().write_text("- just\n- a list\n", encoding="utf-8")
    assert read_host_archetype() is None
    host_archetype_path().write_text("{{{ not yaml", encoding="utf-8")
    assert read_host_archetype() is None


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

    The row is LISTED again (2026-08-22, see test_project_manager_archetype_is_listed)
    and the contract rides with it: the bundle seeds `github.write: true`, so the
    tool binds and the banner stays quiet on a fresh agent."""
    catalog = json.loads((Path(__file__).resolve().parents[1] / "config" / "archetype-catalog.json").read_text())
    pm = next(a for a in catalog["archetypes"] if a["id"] == "project-manager")

    assert "github_create_issue" in pm.get("requires_tools", [])
