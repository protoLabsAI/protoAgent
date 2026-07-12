"""Characterization tests for graph/config.py + graph/config_io.py.

These freeze TODAY's behavior of:
  - LangGraphConfig.from_yaml (the YAML -> dataclass parse, including the
    secret overlay, deep-nesting, list-coercion, key-rename and per-field
    subagent fallbacks)
  - config_to_dict (the dataclass -> nested-dict serialization the UI uses)
  - the config_to_dict -> apply_updates_to_yaml -> from_yaml round-trip

Values are captured by RUNNING the real code (not guessed) and embedded as
literals so a future refactor that drops or mis-parses a field fails loudly.
"""

import dataclasses
import textwrap
from pathlib import Path

import pytest

from graph.config import LangGraphConfig, SubagentDef
from graph.config_io import (
    apply_updates_to_yaml,
    config_to_dict,
    save_yaml_doc,
)

from graph.settings_schema import FIELDS

EXAMPLE_PATH = "config/langgraph-config.example.yaml"

# config_to_dict is FIELDS-driven (graph/config_io.py §A) plus a small set of explicit
# non-FIELDS "legacy" keys (§B). So the SHAPE it emits derives from the schema — only the
# legacy extras and per-field VALUES are worth hand-maintaining. Deriving the shape means
# adding a LangGraphConfig field only touches ONE golden below (FROM_YAML_EXAMPLE_FIELDS,
# the value gate), not four parallel hand-kept lists.
_FIELDS_ATTRS = {f.attr for f in FIELDS}
_FIELDS_SECTIONS = {f.key.split(".", 1)[0] for f in FIELDS}


@pytest.fixture(autouse=True)
def _isolate_from_installed_plugins(monkeypatch):
    """Freeze the CORE config surface, independent of whatever plugins a dev has installed.

    ``from_yaml`` resolves ``plugin_config`` by DISCOVERING installed plugins (ADR 0019),
    and ``config_to_dict`` reflects it — so a dev with any plugin under ``config/plugins/``
    (dev-local, gitignored state) gets extra sections and these goldens spuriously fail,
    while CI (no plugins installed) is green. Plugin-config resolution has its own tests;
    here we pin it empty so the golden means the same thing everywhere."""
    monkeypatch.setattr("graph.config._resolve_plugin_config", lambda *a, **k: {})


# ---------------------------------------------------------------------------
# Golden captures (RUN the real code to confirm; never weaken to force green)
# ---------------------------------------------------------------------------

# from_yaml(example) field map. api_key/auth_token are asserted == "" and
# plugin_config is only asserted to be a dict (see test below), so they are
# OMITTED from this map.
FROM_YAML_EXAMPLE_FIELDS = {
    "a2a_description": "",
    "a2a_require_routable_url": False,
    "a2a_skills": [],
    "acp_agents": {},
    "agent_runtime": "native",
    "developer_channel": "prod",
    "api_base": "http://gateway:4000/v1",
    "audit_middleware": True,
    "autostart_on_boot": False,
    "aux_model": "",
    "background_auto_resume": True,
    "bind_host": "127.0.0.1",
    "cache_warming_enabled": False,
    "cache_warming_interval_seconds": 3300,
    "chat_template_kwargs": None,
    "checkpoint_db_path": "/sandbox/checkpoints.db",
    "checkpoint_harvest_enabled": True,
    "checkpoint_background_keep": 1,
    "checkpoint_keep_per_thread": 5,
    "checkpoint_max_age_days": 30,
    "checkpoint_prune_interval_hours": 6,
    "checkpoint_vacuum": True,
    "commons_path": "",
    "compaction_enabled": True,
    "compaction_keep_messages": 20,
    "compaction_model": "",
    "compaction_trigger": "fraction:0.8",
    "discovery_mdns": False,
    "discovery_port_max": 7910,
    "discovery_port_min": 7860,
    "egress_allowed_hosts": [],
    "embed_model": "qwen3-embedding",
    "transcribe_model": "whisper-1",
    "image_describe_model": "",
    "enforcement_disallowed_tools": [],
    "enforcement_enabled": False,
    "enforcement_rate_limits": {},
    "filesystem_allow_run": True,
    "filesystem_bypass_allowed": True,
    "filesystem_enabled": True,
    "filesystem_projects": [],
    "filesystem_run_requires_approval": True,
    "fleet_autostart": [],
    "fleet_max_warm": 0,
    "fleet_trace_export_enabled": False,
    "fleet_port_base": 7870,
    "fleet_warm_grace_seconds": 0,
    "goal_enabled": True,
    "goal_eval_model": "",
    "goal_max_iterations": 8,
    "goal_no_progress_limit": 3,
    "goal_verify_timeout": 120.0,
    "identity_name": "protoagent",
    "identity_operator": "",
    "identity_org": "",
    "instance_id": "",
    "secrets_manager_enabled": False,
    "secrets_manager_provider": "infisical",
    "secrets_manager_host": "https://us.infisical.com",
    "secrets_manager_project_id": "",
    "secrets_manager_environment": "prod",
    "secrets_manager_path": "/",
    "secrets_manager_recursive": True,
    "secrets_manager_client_id": "",
    "secrets_manager_client_secret": "",
    "secrets_manager_refresh_seconds": 300,
    "secrets_manager_required": False,
    "secrets_manager_override_env": False,
    "secrets_manager_timeout_seconds": 10.0,
    "knowledge_backend": "",
    "knowledge_db_path": "/sandbox/knowledge/agent.db",
    "knowledge_scope": "",
    "knowledge_embedder": "",
    "knowledge_embeddings": False,
    "knowledge_facts": True,
    "knowledge_inject_namespaces": [],
    "knowledge_inject_min_trust": 1,
    "knowledge_hot_write_confirm": False,
    "knowledge_middleware": True,
    "knowledge_top_k": 5,
    "lifecycle_hooks": [],
    "knowledge_vector_k": 20,
    "knowledge_rrf_k": 60,
    "knowledge_min_score": 0.0,
    "knowledge_recall_preview_chars": 1000,
    "knowledge_embed_breaker_threshold": 2,
    "knowledge_embed_breaker_cooldown_s": 300.0,
    "knowledge_chunk_max_chars": 1200,
    "knowledge_chunk_overlap_chars": 150,
    "knowledge_chunk_min_chars": 200,
    "knowledge_contextual_enrichment": False,
    "knowledge_context_max_doc_chars": 12000,
    "knowledge_attach_inline_budget": 8000,
    "llm_max_retries": 2,
    "max_iterations": 50,
    "max_tokens": 32768,
    "media_public": False,
    "media_retention_days": 0,
    "mcp_denylist": [],
    "mcp_enabled": False,
    "mcp_scope": "",
    "mcp_servers": [],
    "mcp_timeout_seconds": 20.0,
    "memory_middleware": True,
    "model_name": "protolabs/reasoning",
    "model_provider": "openai",
    "model_vision": False,
    "operator_allowed_dirs": [],
    "operator_project_dir": "",
    "operator_mcp_enabled": False,
    "operator_mcp_tools": [],
    "operator_mcp_profile": "",
    "plugins_autoupdate_interval_hours": 6,
    "plugins_dir": "",
    "plugins_disabled": [],
    "plugins_enabled": [],
    "plugins_sources_allow": [],
    "plugins_update_policy": {},
    "presence_penalty": None,
    "prompt_cache_enabled": True,
    "prompt_cache_force": False,
    "prompt_cache_ttl": "5m",
    "reasoning_effort": None,
    "repetition_penalty": None,
    "request_timeout": 120,
    "researcher": SubagentDef(
        enabled=True,
        tools=["current_time", "web_search", "fetch_url", "memory_recall", "memory_list"],
        max_turns=40,
        model="",
    ),
    "routing_fallback_models": [],
    "scheduler_enabled": True,
    "security_callback_allowlist": [],
    "skills_db_path": "/sandbox/skills.db",
    "skills_dir": "",
    "skills_enabled": True,
    "skills_scope": "",
    "skills_shared": False,
    "skills_top_k": 5,
    "subagent_max_concurrency": 4,
    "subagent_output_truncate": 6000,
    "telemetry_db_path": "/sandbox/telemetry.db",
    "telemetry_enabled": True,
    "telemetry_retention_days": 90,
    "inbox_retention_days": 90,
    "activity_retention_days": 90,
    "temperature": 0.2,
    "thinking": "",
    "tools_deferred_enabled": False,
    "tools_deferred_keep": [],
    "tools_disabled": [],
    "top_k": -1,
    "top_p": None,
    "workflow_dir": "/sandbox/workflows",
}

# Fields handled by their own dedicated assertions, not the golden map.
_GOLDEN_EXEMPT = {"api_key", "auth_token", "federation_token", "plugin_config"}


def _write_yaml(dir_path: Path, body: str, *, secrets: str | None = None) -> str:
    """Write a langgraph-config.yaml (and optional sibling secrets.yaml) and
    return the config path."""
    cfg = dir_path / "langgraph-config.yaml"
    cfg.write_text(textwrap.dedent(body))
    if secrets is not None:
        (dir_path / "secrets.yaml").write_text(textwrap.dedent(secrets))
    return str(cfg)


# ---------------------------------------------------------------------------
# (a) Golden field map for the shipped example config
# ---------------------------------------------------------------------------


def test_from_yaml_example_golden():
    cfg = LangGraphConfig.from_yaml(EXAMPLE_PATH)

    # Redacted / unpinned fields get dedicated assertions.
    assert cfg.api_key == ""
    assert cfg.auth_token == ""
    assert cfg.federation_token == ""  # ADR 0066 secret — redacted, no example value
    assert isinstance(cfg.plugin_config, dict)

    # Every other dataclass field must match the captured golden exactly.
    # Iterate dataclasses.fields so a dropped or mis-parsed field fails loudly,
    # and the golden map must cover exactly the non-exempt field set.
    field_names = {f.name for f in dataclasses.fields(cfg)}
    assert field_names - _GOLDEN_EXEMPT == set(FROM_YAML_EXAMPLE_FIELDS), (
        "golden field map is out of sync with the dataclass fields"
    )
    for f in dataclasses.fields(cfg):
        if f.name in _GOLDEN_EXEMPT:
            continue
        actual = getattr(cfg, f.name)
        expected = FROM_YAML_EXAMPLE_FIELDS[f.name]
        assert actual == expected, f"{f.name}: {actual!r} != {expected!r}"


# ---------------------------------------------------------------------------
# (b) config_to_dict shape + redaction (shape derived from FIELDS, values gated by (a))
# ---------------------------------------------------------------------------


def test_config_to_dict_shape_and_redaction():
    """config_to_dict's SHAPE derives from FIELDS (graph/config_io.py §A), so we assert the
    top-level sections against the schema + a few representative nested values + secret
    redaction — NOT a frozen copy of the whole nested dict (which drifted on every field
    add). The exhaustive value coverage lives in FROM_YAML_EXAMPLE_FIELDS (the flat view of
    the same cfg) and test_round_trip_preserves_emitted_fields below."""
    cfg = LangGraphConfig.from_yaml(EXAMPLE_PATH)
    d = config_to_dict(cfg)
    # every FIELDS top-level section is emitted (§B legacy keys only add sub-keys under
    # existing sections, never a new top-level one).
    assert _FIELDS_SECTIONS <= set(d.keys())
    # secrets redacted (blank-means-unchanged).
    assert d["model"]["api_key"] == "" and d["auth"]["token"] == ""
    # representative nested values round-trip from the cfg.
    assert d["model"]["name"] == cfg.model_name
    assert d["model"]["temperature"] == cfg.temperature
    assert d["developer"]["channel"] == cfg.developer_channel


# ---------------------------------------------------------------------------
# (c) Round-trip over exactly the attrs config_to_dict emits
# ---------------------------------------------------------------------------


# The non-FIELDS attrs config_to_dict still emits (graph/config_io.py §B) — hand-listed
# because they're not in the schema. Everything else derives from _FIELDS_ATTRS, so a
# newly-added field is round-trip-checked automatically. (This set previously drifted
# silently — developer_channel, checkpoint_vacuum, commons_path, egress_allowed_hosts,
# identity_org, knowledge_scope and skills_scope were all missing from round-trip coverage.)
_LEGACY_EMITTED_ATTRS = {
    "researcher",  # subagents.researcher (a SubagentDef)
    "lifecycle_hooks",  # top-level lifecycle_hooks list (ADR 0074) — a list of dicts, config_io.py §B
    "filesystem_projects",  # filesystem.projects (registry of {name,path,write} dicts)
    "checkpoint_background_keep",
    "knowledge_db_path",
    "knowledge_embed_breaker_threshold",
    "knowledge_embed_breaker_cooldown_s",
    "knowledge_chunk_min_chars",
    "knowledge_context_max_doc_chars",
    "mcp_enabled",
    "mcp_servers",
    "mcp_timeout_seconds",
    "mcp_denylist",
    "skills_enabled",
    "skills_db_path",
    "skills_dir",
    "plugins_enabled",
    "plugins_disabled",
    "plugins_dir",
    "plugins_sources_allow",
    "plugins_update_policy",
    "plugins_autoupdate_interval_hours",
}
# Redacted secrets (api_key / auth_token / federation_token) resolve to "" on both sides.
EMITTED_ATTRS = _FIELDS_ATTRS | _LEGACY_EMITTED_ATTRS


def test_round_trip_preserves_emitted_fields(tmp_path):
    cfg = LangGraphConfig.from_yaml(EXAMPLE_PATH)
    d = config_to_dict(cfg)

    # config_to_dict -> apply into an EMPTY doc -> dump -> reload via from_yaml.
    doc: dict = {}
    apply_updates_to_yaml(doc, d)
    out = tmp_path / "langgraph-config.yaml"
    save_yaml_doc(doc, out)
    reloaded = LangGraphConfig.from_yaml(str(out))

    # Every attr config_to_dict actually emits must survive the round-trip.
    # (Do NOT assert attrs config_to_dict omits — they're free to differ.)
    for attr in EMITTED_ATTRS:
        original = getattr(cfg, attr)
        round_tripped = getattr(reloaded, attr)
        if attr in ("api_key", "auth_token"):
            # Redacted secrets resolve to "" on both sides (no secrets.yaml).
            assert original == "" and round_tripped == "", attr
            continue
        assert round_tripped == original, f"{attr}: {round_tripped!r} != {original!r}"

    # plugin config sections round-trip via plugin_config.
    assert reloaded.plugin_config == cfg.plugin_config


# ---------------------------------------------------------------------------
# (d) One test per special case from the ground truth
# ---------------------------------------------------------------------------


def test_case1a_secret_overlay_wins(tmp_path):
    """Secret beats main YAML beats default — secrets.yaml sibling wins."""
    path = _write_yaml(
        tmp_path,
        """
        model:
          api_key: main_yaml_key
        auth:
          token: main_yaml_token
        """,
        secrets="""
        model:
          api_key: secret_overlay_key
        auth:
          token: secret_overlay_token
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.api_key == "secret_overlay_key"
    assert cfg.auth_token == "secret_overlay_token"


def test_case1b_main_yaml_wins_when_no_secrets_file(tmp_path):
    """No secrets.yaml -> main YAML value wins (over the dataclass default)."""
    path = _write_yaml(
        tmp_path,
        """
        model:
          api_key: main_yaml_key
        auth:
          token: main_yaml_token
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.api_key == "main_yaml_key"
    assert cfg.auth_token == "main_yaml_token"


def test_case2_deep_nesting(tmp_path):
    """prompt_cache.warm.* and tools.deferred.* deep-nested reads."""
    path = _write_yaml(
        tmp_path,
        """
        prompt_cache:
          warm:
            enabled: true
            interval_seconds: 99
        tools:
          deferred:
            enabled: true
            keep:
              - foo
              - bar
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.cache_warming_enabled is True
    assert cfg.cache_warming_interval_seconds == 99
    assert cfg.tools_deferred_enabled is True
    assert cfg.tools_deferred_keep == ["foo", "bar"]


def test_case3_list_coercion_empty_section_is_empty_list(tmp_path):
    """A present-but-empty section parses to None; list(... or []) -> []."""
    path = _write_yaml(
        tmp_path,
        """
        mcp:
          servers:
        tools:
          disabled:
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.mcp_servers == []
    assert cfg.tools_disabled == []


def test_case4_agent_runtime_none_coerces_to_native(tmp_path):
    """'agent_runtime:' parses to None; the 'or "native"' yields 'native'."""
    path = _write_yaml(tmp_path, "agent_runtime:\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.agent_runtime == "native"


def test_case5a_instance_block_id_present(tmp_path):
    """instance.id present -> wins over top-level instance_id."""
    path = _write_yaml(
        tmp_path,
        """
        instance:
          id: from_instance_block
        instance_id: from_toplevel
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.instance_id == "from_instance_block"


def test_case5b_only_toplevel_instance_id(tmp_path):
    """Only top-level instance_id present."""
    path = _write_yaml(tmp_path, "instance_id: from_toplevel\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.instance_id == "from_toplevel"


def test_case5c_empty_instance_id_falls_through(tmp_path):
    """Empty-string instance.id is falsy -> falls through to top-level."""
    path = _write_yaml(
        tmp_path,
        """
        instance:
          id: ""
        instance_id: from_toplevel
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.instance_id == "from_toplevel"


def test_case6a_cross_section_both_present(tmp_path):
    """Enabled flags from middleware.*; tool lists from own top-level sections."""
    path = _write_yaml(
        tmp_path,
        """
        middleware:
          enforcement: true
        enforcement:
          disallowed_tools:
            - dangerous_tool
          rate_limits:
            web_search: 5
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.enforcement_enabled is True
    assert cfg.enforcement_disallowed_tools == ["dangerous_tool"]
    assert cfg.enforcement_rate_limits == {"web_search": 5}


def test_case6b_middleware_flags_only_no_toplevel_sections(tmp_path):
    """Flags read True from middleware.*; lists default to []/{} because their
    OWN top-level sections are absent (proves the split)."""
    path = _write_yaml(
        tmp_path,
        """
        middleware:
          enforcement: true
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.enforcement_enabled is True
    assert cfg.enforcement_disallowed_tools == []
    assert cfg.enforcement_rate_limits == {}


def test_case7_key_rename_max_retries(tmp_path):
    """YAML model.max_retries -> attr llm_max_retries."""
    path = _write_yaml(tmp_path, "model:\n  max_retries: 7\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.llm_max_retries == 7


def test_case8a_researcher_partial_only_model(tmp_path):
    """Only model overridden; other fields fall back to RESEARCHER_CONFIG."""
    path = _write_yaml(
        tmp_path,
        """
        subagents:
          researcher:
            model: custom-model
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.researcher == SubagentDef(
        enabled=True,
        tools=["current_time", "web_search", "fetch_url", "memory_recall", "memory_list", "memory_ingest"],
        max_turns=40,
        model="custom-model",
    )


def test_case8b_researcher_empty_dict(tmp_path):
    """Present-but-empty dict: override branch runs, every field hits the
    registry default."""
    path = _write_yaml(tmp_path, "subagents:\n  researcher: {}\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.researcher == SubagentDef(
        enabled=True,
        tools=["current_time", "web_search", "fetch_url", "memory_recall", "memory_list", "memory_ingest"],
        max_turns=40,
        model="",
    )


def test_case8c_researcher_absent(tmp_path):
    """researcher absent -> override branch skipped, default_factory value used.
    Identical to 8b."""
    path = _write_yaml(tmp_path, "model:\n  name: foo\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.researcher == SubagentDef(
        enabled=True,
        tools=["current_time", "web_search", "fetch_url", "memory_recall", "memory_list", "memory_ingest"],
        max_turns=40,
        model="",
    )


def test_case9_empty_a2a_section_handled_via_or(tmp_path):
    """'a2a:' with no body parses to None; data.get('a2a') or {} protects the
    subsequent .get() calls."""
    path = _write_yaml(tmp_path, "a2a:\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.a2a_skills == []
    assert cfg.a2a_description == ""
    assert cfg.a2a_require_routable_url is False


# ---------------------------------------------------------------------------
# (e) Reviewer-found gaps — real save/merge path, secret edge, coercions
# ---------------------------------------------------------------------------


def test_real_merge_path_preserves_omitted_and_overwrites_emitted(tmp_path):
    """The REAL save path merges config_to_dict INTO the existing doc (not an
    empty one). config_to_dict-EMITTED keys are overwritten; sections it OMITS
    and unknown keys are preserved. (Behavior captured live.)

    B1 PR-3: config_to_dict is now FIELDS-complete, so the formerly-omitted
    ``goal`` section is now emitted. ``a2a`` is a real still-omitted section
    (no FIELDS keys, parsed by from_yaml) — it now stands in as the
    omitted-but-preserved case."""
    cfg = LangGraphConfig.from_yaml(EXAMPLE_PATH)
    doc = {
        "a2a": {"description": "kept_desc"},  # omitted by config_to_dict
        "model": {"name": "OLD_NAME", "extra_key": "keep_me"},  # emitted section + unknown key
    }
    apply_updates_to_yaml(doc, config_to_dict(cfg))
    out = tmp_path / "langgraph-config.yaml"
    save_yaml_doc(doc, out)
    reloaded = LangGraphConfig.from_yaml(str(out))

    assert reloaded.a2a_description == "kept_desc"  # omitted section preserved
    assert reloaded.model_name == "protolabs/reasoning"  # emitted key overwritten
    assert doc["model"]["extra_key"] == "keep_me"  # unknown key untouched by the merge


def test_empty_string_secret_falls_through_to_main_yaml(tmp_path):
    """An empty-string secret is falsy, so `secret or main.get(...)` falls
    through to the main-YAML value instead of clobbering it with ""."""
    path = _write_yaml(
        tmp_path,
        "model:\n  api_key: main_key\n",
        secrets="model:\n  api_key: ''\n",
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.api_key == "main_key"


def test_agent_runtime_nonstring_coerces_to_str(tmp_path):
    """agent_runtime = str(value or "native") coerces a non-string to str."""
    path = _write_yaml(tmp_path, "agent_runtime: 123\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.agent_runtime == "123"


def test_a2a_require_routable_url_bool_coercion(tmp_path):
    """a2a.require_routable_url = bool(value) coerces truthy/falsy ints."""
    d1 = tmp_path / "truthy"
    d1.mkdir()
    d0 = tmp_path / "falsy"
    d0.mkdir()
    c1 = LangGraphConfig.from_yaml(_write_yaml(d1, "a2a:\n  require_routable_url: 1\n"))
    c0 = LangGraphConfig.from_yaml(_write_yaml(d0, "a2a:\n  require_routable_url: 0\n"))
    assert c1.a2a_require_routable_url is True
    assert c0.a2a_require_routable_url is False


def test_researcher_explicit_tools_override_replaces(tmp_path):
    """An explicit researcher.tools list REPLACES the registry default (not merged)."""
    path = _write_yaml(tmp_path, "subagents:\n  researcher:\n    tools: [only_this]\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.researcher.tools == ["only_this"]


def test_plugins_sources_empty_section_is_empty_list(tmp_path):
    """plugins.sources present-but-empty -> (sources or {}).get('allow', []) -> []."""
    path = _write_yaml(tmp_path, "plugins:\n  sources:\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.plugins_sources_allow == []


def test_null_top_level_section_falls_back_to_defaults(tmp_path):
    """A whole section commented out parses to `routing: null`; the loader must
    treat it as absent (use defaults), not crash on `None.get(...)`. Guards the
    example, which a user edits by commenting blocks out."""
    path = _write_yaml(tmp_path, "model:\nrouting:\ngoal:\n")  # all three null
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.api_base == "http://gateway:4000/v1"  # model.* defaults
    assert cfg.routing_fallback_models == []  # routing.* defaults
    assert cfg.goal_enabled is True  # goal.* defaults


def test_plugins_disabled_and_sources_allow_survive_config_to_dict():
    """N6 (2026-06-10 prod-readiness audit): config_to_dict emitted only
    plugins.{enabled, dir}, dropping `disabled` + `sources.allow`. The YAML file
    round-trip was never lossy (apply_updates_to_yaml merges in place and never
    deletes absent keys) — the DICT was: any consumer treating it as the complete
    config lost them, and the Settings UI could never display/edit them. This
    pins the full plugins.* section round-tripping through from_dict ->
    config_to_dict with NON-default values."""
    cfg = LangGraphConfig.from_dict(
        {
            "plugins": {
                "enabled": ["alpha"],
                "disabled": ["beta"],
                "dir": "/custom/plugins",
                "sources": {"allow": ["github.com/protolabsai/*"]},
            },
        }
    )
    d = config_to_dict(cfg)
    assert d["plugins"] == {
        "enabled": ["alpha"],
        "disabled": ["beta"],
        "dir": "/custom/plugins",
        "sources": {"allow": ["github.com/protolabsai/*"]},
        "update_policy": {},
        "autoupdate_interval_hours": 6,
    }
