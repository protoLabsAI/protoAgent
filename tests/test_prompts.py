"""System-prompt SOUL resolution — a fleet member must load ITS OWN persona.

Regression guard for the member-identity bug: ``build_system_prompt`` used to read
``{workspace}/SOUL.md`` with ``workspace`` defaulting to the hub root ``/sandbox``. A
fleet member (spawned directly by the supervisor, not via entrypoint.sh) inherited that
default and so loaded the HUB's SOUL file — a placeholder for the member — leaving it with
no identity. The fix reads the instance's canonical ``config/SOUL.md``
(``instance_paths().soul_path``, PROTOAGENT_HOME-aware) first.
"""

from __future__ import annotations

from pathlib import Path

from graph.prompts import build_system_prompt


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_member_loads_own_config_soul_over_hub_workspace(monkeypatch, tmp_path):
    """The instance's own config/SOUL.md wins over the (hub-default) {workspace}/SOUL.md."""
    home = tmp_path / "member"
    _write(home / "config" / "SOUL.md", "# Identity\nI am Matt, the design-system engineer.")
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    # The hub's default workspace carries a DIFFERENT (placeholder) SOUL.
    hub = tmp_path / "hub"
    _write(hub / "SOUL.md", "# Soul\nReplace this file.")

    prompt = build_system_prompt(workspace=str(hub), include_subagents=False)

    assert "I am Matt, the design-system engineer." in prompt
    assert "Replace this file." not in prompt


def test_falls_back_to_workspace_soul_when_no_instance_soul(monkeypatch, tmp_path):
    """Backward-compat: with no instance config/SOUL.md, the legacy {workspace}/SOUL.md is used."""
    home = tmp_path / "member"
    (home / "config").mkdir(parents=True)  # instance root exists but no SOUL.md
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    hub = tmp_path / "hub"
    _write(hub / "SOUL.md", "# Identity\nLegacy runtime persona.")

    prompt = build_system_prompt(workspace=str(hub), include_subagents=False)

    assert "Legacy runtime persona." in prompt
