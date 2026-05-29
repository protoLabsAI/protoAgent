"""Tests for graph/config_io.py — the plumbing behind the live-edit drawer.

Critical invariants:

- YAML round-trip preserves unknown top-level sections (forks add
  these; silently dropping them on save would be a footgun).
- ``apply_updates_to_yaml`` mutates only the keys you pass and leaves
  siblings alone.
- ``validate_config_dict`` catches range / type errors before disk
  writes.
- ``read_soul`` / ``write_soul`` handles the dual-location contract
  (/sandbox/SOUL.md as runtime, config/SOUL.md as source).
- ``list_gateway_models`` returns a readable error message rather
  than raising — the UI shows this string directly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ── YAML round-trip ──────────────────────────────────────────────────────────


def test_yaml_round_trip_preserves_unknown_keys(tmp_path: Path) -> None:
    """Forks add custom top-level sections (the shipped YAML already
    has ``memory`` and ``skills`` that the dataclass doesn't model).
    Round-tripping through load_yaml_doc + save_yaml_doc must leave
    them intact."""
    from graph import config_io

    yaml_path = tmp_path / "langgraph-config.yaml"
    yaml_path.write_text(
        "model:\n"
        "  name: test-model\n"
        "  temperature: 0.5\n"
        "memory:\n"
        "  path: /custom/memory\n"
        "  max_sessions: 42\n"
        "custom_section:\n"
        "  arbitrary_key: arbitrary_value\n"
    )

    doc = config_io.load_yaml_doc(yaml_path)
    config_io.save_yaml_doc(doc, yaml_path)

    reloaded = config_io.load_yaml_doc(yaml_path)
    assert reloaded["memory"]["path"] == "/custom/memory"
    assert reloaded["memory"]["max_sessions"] == 42
    assert reloaded["custom_section"]["arbitrary_key"] == "arbitrary_value"


def test_apply_updates_merges_shallowly(tmp_path: Path) -> None:
    """Updating model.temperature must NOT clobber model.name or
    other model.* fields."""
    from graph import config_io

    yaml_path = tmp_path / "c.yaml"
    yaml_path.write_text(
        "model:\n"
        "  name: original-model\n"
        "  temperature: 0.1\n"
        "  api_base: http://original\n"
    )

    doc = config_io.load_yaml_doc(yaml_path)
    config_io.apply_updates_to_yaml(doc, {"model": {"temperature": 0.9}})
    config_io.save_yaml_doc(doc, yaml_path)

    reloaded = config_io.load_yaml_doc(yaml_path)
    assert reloaded["model"]["name"] == "original-model"
    assert reloaded["model"]["api_base"] == "http://original"
    assert reloaded["model"]["temperature"] == 0.9


def test_apply_updates_adds_missing_sections(tmp_path: Path) -> None:
    from graph import config_io

    yaml_path = tmp_path / "c.yaml"
    yaml_path.write_text("model:\n  name: x\n")
    doc = config_io.load_yaml_doc(yaml_path)

    config_io.apply_updates_to_yaml(
        doc,
        {"middleware": {"audit": True, "memory": False}},
    )

    assert doc["middleware"]["audit"] is True
    assert doc["middleware"]["memory"] is False
    assert doc["model"]["name"] == "x"


def test_apply_updates_nested_researcher(tmp_path: Path) -> None:
    """subagents.researcher.tools is a list, subagents.researcher.enabled
    is a bool — both must land in the right nested slot."""
    from graph import config_io

    yaml_path = tmp_path / "c.yaml"
    yaml_path.write_text("subagents:\n  researcher:\n    enabled: false\n")
    doc = config_io.load_yaml_doc(yaml_path)

    config_io.apply_updates_to_yaml(
        doc,
        {"subagents": {"researcher": {"enabled": True, "tools": ["current_time", "calculator"]}}},
    )

    assert doc["subagents"]["researcher"]["enabled"] is True
    assert list(doc["subagents"]["researcher"]["tools"]) == ["current_time", "calculator"]


# ── config_to_dict ───────────────────────────────────────────────────────────


def test_config_to_dict_mirrors_yaml_shape() -> None:
    """The UI works with the dict shape; the YAML schema uses the
    same paths. Keep them in lockstep so round-tripping through
    apply_updates_to_yaml works without path rewrites."""
    from graph.config import LangGraphConfig
    from graph.config_io import config_to_dict

    cfg = LangGraphConfig()
    d = config_to_dict(cfg)

    # Top-level schema surface — all the sections the YAML exposes.
    # Adding a new section here without updating config_to_dict would
    # strand fork-added fields outside the drawer's round-trip.
    assert set(d.keys()) == {
        "model", "subagents", "middleware", "knowledge", "skills", "mcp", "plugins",
        "identity", "auth", "runtime", "operator",
    }
    assert d["model"]["name"] == cfg.model_name
    assert d["model"]["temperature"] == cfg.temperature
    # Secrets are redacted out of the UI-facing dict.
    assert d["model"]["api_key"] == ""
    assert d["auth"]["token"] == ""
    assert d["subagents"]["researcher"]["tools"] == list(cfg.researcher.tools)
    assert d["middleware"]["audit"] == cfg.audit_middleware
    assert d["knowledge"]["top_k"] == cfg.knowledge_top_k
    assert d["skills"]["enabled"] == cfg.skills_enabled
    assert d["skills"]["db_path"] == cfg.skills_db_path
    assert d["identity"]["name"] == cfg.identity_name
    assert d["runtime"]["autostart_on_boot"] == cfg.autostart_on_boot
    assert d["operator"]["allowed_dirs"] == list(cfg.operator_allowed_dirs)


# ── validate_config_dict ─────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_value,expected_error_fragment", [
    ({"model": {"temperature": 3.0}}, "temperature"),
    ({"model": {"temperature": -0.1}}, "temperature"),
    ({"model": {"max_tokens": 0}}, "max_tokens"),
    ({"model": {"max_iterations": 0}}, "max_iterations"),
    ({"subagents": {"researcher": {"max_turns": 0}}}, "max_turns"),
    ({"subagents": {"researcher": {"tools": "not-a-list"}}}, "list"),
    ({"knowledge": {"top_k": 0}}, "top_k"),
    ({"operator": {"allowed_dirs": "not-a-list"}}, "allowed_dirs"),
    ({"operator": {"allowed_dirs": [1, 2]}}, "allowed_dirs"),
])
def test_validate_rejects_bad_values(bad_value, expected_error_fragment):
    from graph.config_io import validate_config_dict
    ok, err = validate_config_dict(bad_value)
    assert not ok
    assert expected_error_fragment in err


def test_validate_accepts_happy_path():
    from graph.config_io import config_to_dict, validate_config_dict
    from graph.config import LangGraphConfig

    ok, err = validate_config_dict(config_to_dict(LangGraphConfig()))
    assert ok, err


def test_from_yaml_reads_operator_allowed_dirs(tmp_path: Path) -> None:
    """The operator allowlist round-trips through the YAML schema so a
    settings reload (not just first-run setup) can change it."""
    from graph.config import LangGraphConfig

    p = tmp_path / "langgraph-config.yaml"
    p.write_text(
        "operator:\n"
        "  allowed_dirs:\n"
        "    - /home/kj/projects/foo\n"
        "    - /home/kj/projects/bar\n"
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.operator_allowed_dirs == [
        "/home/kj/projects/foo",
        "/home/kj/projects/bar",
    ]


def test_from_yaml_operator_allowed_dirs_defaults_empty(tmp_path: Path) -> None:
    from graph.config import LangGraphConfig

    p = tmp_path / "langgraph-config.yaml"
    p.write_text("model:\n  name: test\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.operator_allowed_dirs == []


# ── ensure_live_config (template → live bootstrap) ───────────────────────────


def test_ensure_live_config_seeds_from_example(monkeypatch, tmp_path: Path) -> None:
    from graph import config_io

    example = tmp_path / "langgraph-config.example.yaml"
    live = tmp_path / "langgraph-config.yaml"
    example.write_text("model:\n  name: from-template\n")
    monkeypatch.setattr(config_io, "CONFIG_EXAMPLE_PATH", example)
    monkeypatch.setattr(config_io, "CONFIG_YAML_PATH", live)

    assert config_io.ensure_live_config() is True
    assert live.exists()
    assert live.read_text() == example.read_text()


def test_ensure_live_config_does_not_clobber_existing(monkeypatch, tmp_path: Path) -> None:
    from graph import config_io

    example = tmp_path / "langgraph-config.example.yaml"
    live = tmp_path / "langgraph-config.yaml"
    example.write_text("model:\n  name: from-template\n")
    live.write_text("model:\n  name: user-edited\n")
    monkeypatch.setattr(config_io, "CONFIG_EXAMPLE_PATH", example)
    monkeypatch.setattr(config_io, "CONFIG_YAML_PATH", live)

    assert config_io.ensure_live_config() is False
    assert "user-edited" in live.read_text()  # untouched


def test_ensure_live_config_noop_without_example(monkeypatch, tmp_path: Path) -> None:
    from graph import config_io

    monkeypatch.setattr(config_io, "CONFIG_EXAMPLE_PATH", tmp_path / "absent.example.yaml")
    monkeypatch.setattr(config_io, "CONFIG_YAML_PATH", tmp_path / "langgraph-config.yaml")

    assert config_io.ensure_live_config() is False
    assert not (tmp_path / "langgraph-config.yaml").exists()


# ── SOUL.md dual-path ────────────────────────────────────────────────────────


def test_read_soul_falls_back_to_source(monkeypatch, tmp_path: Path) -> None:
    """When /sandbox/SOUL.md doesn't exist (local dev), fall through
    to the repo config dir so drawer edits are still visible."""
    from graph import config_io

    # Point the runtime path at an unreachable location so the source
    # fallback is exercised.
    fake_runtime = tmp_path / "nonexistent" / "SOUL.md"
    fake_source = tmp_path / "SOUL-source.md"
    fake_source.write_text("from source", encoding="utf-8")

    monkeypatch.setattr(config_io, "SOUL_RUNTIME_PATH", fake_runtime)
    monkeypatch.setattr(config_io, "SOUL_SOURCE_PATH", fake_source)

    assert config_io.read_soul() == "from source"


def test_read_soul_prefers_runtime(monkeypatch, tmp_path: Path) -> None:
    from graph import config_io

    runtime = tmp_path / "runtime" / "SOUL.md"
    runtime.parent.mkdir()
    runtime.write_text("runtime wins", encoding="utf-8")
    source = tmp_path / "SOUL-source.md"
    source.write_text("source loses", encoding="utf-8")

    monkeypatch.setattr(config_io, "SOUL_RUNTIME_PATH", runtime)
    monkeypatch.setattr(config_io, "SOUL_SOURCE_PATH", source)

    assert config_io.read_soul() == "runtime wins"


def test_write_soul_writes_source_always(monkeypatch, tmp_path: Path) -> None:
    """The source-of-truth write (config/SOUL.md) must always succeed;
    the runtime write is best-effort (skipped when /sandbox missing)."""
    from graph import config_io

    # Runtime points at a path whose parent doesn't exist — should skip
    # gracefully.
    runtime = tmp_path / "no-sandbox-here" / "SOUL.md"
    source = tmp_path / "src" / "SOUL.md"

    monkeypatch.setattr(config_io, "SOUL_RUNTIME_PATH", runtime)
    monkeypatch.setattr(config_io, "SOUL_SOURCE_PATH", source)

    written = config_io.write_soul("hello world")
    assert source in written
    assert runtime not in written
    assert source.read_text() == "hello world"


def test_write_soul_writes_both_when_runtime_parent_exists(
    monkeypatch, tmp_path: Path,
) -> None:
    from graph import config_io

    runtime_dir = tmp_path / "sandbox"
    runtime_dir.mkdir()
    runtime = runtime_dir / "SOUL.md"
    source = tmp_path / "src" / "SOUL.md"

    monkeypatch.setattr(config_io, "SOUL_RUNTIME_PATH", runtime)
    monkeypatch.setattr(config_io, "SOUL_SOURCE_PATH", source)

    written = config_io.write_soul("dual write")
    assert runtime in written
    assert source in written
    assert runtime.read_text() == "dual write"
    assert source.read_text() == "dual write"


# ── Gateway model listing ────────────────────────────────────────────────────


def test_list_gateway_models_success(monkeypatch):
    from graph import config_io

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "data": [
            {"id": "model-b"},
            {"id": "model-a"},
            {"id": "model-c"},
        ],
    }

    fake_client = MagicMock()
    fake_client.__enter__ = lambda self: fake_client
    fake_client.__exit__ = lambda *args: None
    fake_client.get.return_value = fake_response

    monkeypatch.setattr("httpx.Client", lambda **kw: fake_client)

    models, err = config_io.list_gateway_models("http://gateway:4000/v1", "test-key")
    assert err == ""
    assert models == ["model-a", "model-b", "model-c"]  # sorted
    called_url = fake_client.get.call_args[0][0]
    assert called_url == "http://gateway:4000/v1/models"


def test_list_gateway_models_empty_base_returns_error():
    from graph.config_io import list_gateway_models

    models, err = list_gateway_models("", "key")
    assert models == []
    assert "api_base" in err


def test_list_gateway_models_http_error(monkeypatch):
    from graph import config_io

    fake_client = MagicMock()
    fake_client.__enter__ = lambda self: fake_client
    fake_client.__exit__ = lambda *args: None
    fake_client.get.side_effect = httpx.ConnectError("no route to host")

    monkeypatch.setattr("httpx.Client", lambda **kw: fake_client)

    models, err = config_io.list_gateway_models("http://bad-host/v1")
    assert models == []
    assert "connection failed" in err


def test_list_gateway_models_bad_status(monkeypatch):
    from graph import config_io

    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = "unauthorized"

    fake_client = MagicMock()
    fake_client.__enter__ = lambda self: fake_client
    fake_client.__exit__ = lambda *args: None
    fake_client.get.return_value = fake_response

    monkeypatch.setattr("httpx.Client", lambda **kw: fake_client)

    models, err = config_io.list_gateway_models("http://x/v1", "bad-key")
    assert models == []
    assert "401" in err


# ── list_available_tools ─────────────────────────────────────────────────────


def test_list_available_tools_returns_starter_set():
    from graph.config_io import list_available_tools

    names = list_available_tools()
    # Lock in the template's starter set — forks replace these but
    # the drawer's CheckboxGroup populates from this call, so the
    # contract is "return tool names in a stable list".
    assert "current_time" in names
    assert "calculator" in names
    assert "web_search" in names
    assert "fetch_url" in names
    # Memory + scheduler tools appear in the wizard checklist even
    # when no store / scheduler has been constructed yet — otherwise
    # the user couldn't enable them on a fresh boot.
    assert "memory_ingest" in names
    assert "schedule_task" in names
    assert "list_schedules" in names
    assert "cancel_schedule" in names
    assert all(isinstance(n, str) for n in names)
    # No duplicates — list_available_tools dedupes between the
    # backend-bound tools and the static name lists.
    assert len(names) == len(set(names))


# ── Setup wizard marker ─────────────────────────────────────────────────────


def test_setup_marker_lifecycle(monkeypatch, tmp_path):
    """Marker presence = wizard skipped. Mark → present. Reset → gone.
    Reset on a missing marker is a no-op, not an error."""
    from graph import config_io

    marker = tmp_path / ".setup-complete"
    monkeypatch.setattr(config_io, "SETUP_MARKER_PATH", marker)

    assert config_io.is_setup_complete() is False

    config_io.mark_setup_complete()
    assert config_io.is_setup_complete() is True
    assert marker.exists()

    config_io.mark_setup_complete()  # idempotent
    assert config_io.is_setup_complete() is True

    config_io.reset_setup()
    assert config_io.is_setup_complete() is False

    config_io.reset_setup()  # no-op on missing marker — doesn't raise


def test_mark_setup_complete_creates_parent_dir(monkeypatch, tmp_path):
    """If config/ doesn't exist yet, mark_setup_complete must create
    it — otherwise a fresh clone with a pristine filesystem fails
    on first wizard run."""
    from graph import config_io

    marker = tmp_path / "fresh" / "config" / ".setup-complete"
    monkeypatch.setattr(config_io, "SETUP_MARKER_PATH", marker)

    config_io.mark_setup_complete()
    assert marker.exists()


# ── SOUL.md presets ─────────────────────────────────────────────────────────


def test_list_soul_presets_returns_shipped_starters():
    """The template must ship four starter presets so the wizard's
    dropdown is useful on day one. Add a file to config/soul-presets/
    and it should appear here automatically — no registry."""
    from graph.config_io import list_soul_presets

    presets = list_soul_presets()
    assert "generic-assistant" in presets
    assert "research" in presets
    assert "coding" in presets
    assert "blank" in presets


def test_list_soul_presets_sorted():
    from graph.config_io import list_soul_presets

    presets = list_soul_presets()
    assert presets == sorted(presets)


def test_read_soul_preset_returns_content():
    from graph.config_io import read_soul_preset

    content = read_soul_preset("research")
    assert "research" in content.lower()
    assert content.strip().startswith("#")  # markdown h1


def test_read_soul_preset_unknown_returns_empty():
    """Unknown preset names must return '' not raise — the wizard
    treats empty as 'user didn't pick a preset, keep textarea as-is'."""
    from graph.config_io import read_soul_preset

    assert read_soul_preset("not-a-real-preset") == ""
    assert read_soul_preset("") == ""


@pytest.mark.parametrize("malicious", [
    "../secret",
    "../../etc/passwd",
    "../../../etc/passwd",
    "subdir/../../../outside",
    "/etc/hosts",
    "..",
    "../../graph/config",  # try to read a real repo file via ../../
])
def test_read_soul_preset_rejects_path_traversal(malicious):
    """CRITICAL: the preset name must not let a caller escape
    ``config/soul-presets/``. Every ``..`` or absolute path
    should return empty string, not read an arbitrary .md file
    elsewhere on disk."""
    from graph.config_io import read_soul_preset

    assert read_soul_preset(malicious) == ""


def test_list_soul_presets_missing_dir_returns_empty(monkeypatch, tmp_path):
    """If a fork accidentally deletes the presets dir, the wizard
    should render an empty dropdown, not crash."""
    from graph import config_io

    fake = tmp_path / "does-not-exist"
    monkeypatch.setattr(config_io, "PRESETS_DIR", fake)

    assert config_io.list_soul_presets() == []
