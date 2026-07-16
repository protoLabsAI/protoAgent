"""A settings save whose reload fails must leave the config exactly as it was.

The bug this pins (found live): `_apply_settings_changes` committed the YAML and *then*
rebuilt the graph. A failed rebuild left the new config on disk while the process kept
serving the old graph — so `GET /api/config` and `langgraph-config.yaml` disagreed, and the
NEXT restart booted the very config the rebuild had just rejected.

Reproduced end-to-end before the fix: an ACP-only instance (no gateway key — `create_llm`'s
ACP fallback covers `acp:*`, but `native` needs a real key) was switched to `native`; the
rebuild failed with "Missing credentials", `agent_runtime: native` was written anyway, and
the instance then would not boot at all (`create_llm` raises during `create_agent_graph`,
process exits 1). Recovery required hand-editing YAML — the console can't help, because the
console needs the server that won't start.

`_reload_langgraph_agent` returning False always means nothing was committed (both of its
failure exits return before `STATE.graph = new_graph`), so rolling the YAML back is the
correct response, not a guess.
"""

from pathlib import Path

import pytest
import yaml as _yaml


@pytest.fixture
def isolated_config(monkeypatch, tmp_path: Path):
    """Point every config layer at tmp files so a save can't touch the real instance."""
    import graph.config_io as cio

    leaf = tmp_path / "langgraph-config.yaml"
    secrets = tmp_path / "secrets.yaml"
    host = tmp_path / "host-config.yaml"

    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: secrets)

    import infra.paths as paths

    monkeypatch.setattr(paths, "host_config_path", lambda: host, raising=False)
    return leaf, secrets, host


def _read(p: Path):
    return _yaml.safe_load(p.read_text()) if p.exists() else None


def test_failed_reload_rolls_the_config_back(monkeypatch, isolated_config) -> None:
    leaf, _secrets, _host = isolated_config
    import server.agent_init as ai

    leaf.write_text("agent_runtime: acp:proto\nmodel:\n  name: m\n")
    before = leaf.read_bytes()

    # The exact live failure: the rebuild can't construct a gateway model without a key.
    monkeypatch.setattr(
        ai, "_reload_langgraph_agent", lambda: (False, "graph rebuild failed: Missing credentials")
    )

    ok, messages = ai._apply_settings_changes(config={"agent_runtime": "native"})

    assert ok is False
    assert leaf.read_bytes() == before, "the rejected config must not survive on disk"
    assert _read(leaf)["agent_runtime"] == "acp:proto"
    assert any("rolled back" in m for m in messages), messages


def test_a_rolled_back_save_does_not_also_claim_it_saved(monkeypatch, isolated_config) -> None:
    """The operator reads these messages. "config saved · rebuild failed · rolled back" says
    the config both did and didn't save; the write announcement is retroactively false once
    the undo lands, so it goes — the failure REASON stays."""
    leaf, _secrets, _host = isolated_config
    import server.agent_init as ai

    leaf.write_text("agent_runtime: acp:proto\n")
    monkeypatch.setattr(
        ai, "_reload_langgraph_agent", lambda: (False, "graph rebuild failed: Missing credentials")
    )

    _ok, messages = ai._apply_settings_changes(config={"agent_runtime": "native"})

    assert "config saved" not in messages, messages
    assert any("Missing credentials" in m for m in messages), "keep the reason it failed"
    assert messages[-1].startswith("rolled back"), messages


def test_successful_reload_keeps_the_write(monkeypatch, isolated_config) -> None:
    leaf, _secrets, _host = isolated_config
    import server.agent_init as ai

    leaf.write_text("agent_runtime: acp:proto\n")
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (True, "reloaded"))

    ok, messages = ai._apply_settings_changes(config={"agent_runtime": "native"})

    assert ok is True
    assert _read(leaf)["agent_runtime"] == "native"
    assert not any("rolled back" in m for m in messages), messages


def test_rollback_deletes_a_secrets_file_the_write_created(monkeypatch, isolated_config) -> None:
    """A first-ever secret must not be left behind by a save that was rejected."""
    leaf, secrets, _host = isolated_config
    import server.agent_init as ai

    leaf.write_text("model:\n  name: m\n")
    assert not secrets.exists()
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (False, "graph rebuild failed: boom"))

    ok, _ = ai._apply_settings_changes(config={"model": {"api_key": "sk-new"}})

    assert ok is False
    assert not secrets.exists(), "a secrets file created by a rejected save must be removed"


def test_rollback_preserves_secrets_file_mode(monkeypatch, isolated_config) -> None:
    """Restoring secrets.yaml must not widen its 0600 permissions."""
    leaf, secrets, _host = isolated_config
    import server.agent_init as ai

    leaf.write_text("model:\n  name: m\n")
    secrets.write_text("model:\n  api_key: sk-old\n")
    secrets.chmod(0o600)
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (False, "graph rebuild failed: boom"))

    ai._apply_settings_changes(config={"model": {"api_key": "sk-new"}})

    assert _read(secrets) == {"model": {"api_key": "sk-old"}}
    assert secrets.stat().st_mode & 0o777 == 0o600


def test_failed_reset_rolls_back_too(monkeypatch, isolated_config) -> None:
    """Reset-to-inherited shares the write-then-reload shape, so it shares the fix:
    dropping an override rebuilds against the INHERITED value, which can fail just as
    easily as setting one."""
    leaf, _secrets, _host = isolated_config
    import server.agent_init as ai

    leaf.write_text("agent_runtime: acp:proto\nmodel:\n  name: m\n")
    before = leaf.read_bytes()
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (False, "graph rebuild failed: boom"))

    ok, messages = ai._reset_settings_keys(["agent_runtime"])

    assert ok is False
    assert leaf.read_bytes() == before, "the popped key must come back"
    assert any("rolled back" in m for m in messages), messages


def test_soul_is_not_rolled_back(monkeypatch, isolated_config, tmp_path: Path) -> None:
    """SOUL is authored prose, deliberately outside the rollback: reverting an operator's
    persona edit because an unrelated rebuild failed would destroy work to fix a mismatch
    the persona didn't cause."""
    leaf, _secrets, _host = isolated_config
    import server.agent_init as ai

    leaf.write_text("model:\n  name: m\n")
    written: list[str] = []
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (False, "graph rebuild failed: boom"))
    monkeypatch.setattr(
        "graph.config_io.write_soul", lambda s: (written.append(s), [tmp_path / "SOUL.md"])[1]
    )

    ok, _ = ai._apply_settings_changes(soul="# New persona")

    assert ok is False
    assert written == ["# New persona"], "the SOUL write stands even when the reload fails"


def test_pure_reload_failure_touches_nothing(monkeypatch, isolated_config) -> None:
    """A bare reload has no write to undo — it must not invent one."""
    leaf, _secrets, _host = isolated_config
    import server.agent_init as ai

    leaf.write_text("model:\n  name: m\n")
    before = leaf.read_bytes()
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (False, "config load failed: boom"))

    ok, messages = ai._apply_settings_changes()

    assert ok is False
    assert leaf.read_bytes() == before
    assert not any("rolled back" in m for m in messages), messages
