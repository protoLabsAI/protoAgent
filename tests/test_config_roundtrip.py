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

from graph.config import LangGraphConfig, SubagentDef
from graph.config_io import (
    apply_updates_to_yaml,
    config_to_dict,
    save_yaml_doc,
)

EXAMPLE_PATH = "config/langgraph-config.example.yaml"


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
    "agent_runtime": "native",
    "api_base": "http://gateway:4000/v1",
    "audit_middleware": True,
    "autostart_on_boot": False,
    "aux_model": "",
    "bind_host": "127.0.0.1",
    "cache_warming_enabled": False,
    "cache_warming_interval_seconds": 3300,
    "chat_template_kwargs": None,
    "checkpoint_db_path": "/sandbox/checkpoints.db",
    "checkpoint_harvest_enabled": True,
    "checkpoint_keep_per_thread": 5,
    "checkpoint_max_age_days": 30,
    "checkpoint_prune_interval_hours": 6,
    "commons_path": "",
    "compaction_enabled": True,
    "compaction_keep_messages": 20,
    "compaction_model": "",
    "compaction_trigger": "fraction:0.8",
    "discovery_mdns": True,
    "discovery_port_max": 7910,
    "discovery_port_min": 7860,
    "egress_allowed_hosts": [],
    "embed_model": "qwen3-embedding",
    "transcribe_model": "whisper-1",
    "enforcement_disallowed_tools": [],
    "enforcement_enabled": False,
    "enforcement_rate_limits": {},
    "execute_code_enabled": False,
    "execute_code_output_truncate": 6000,
    "execute_code_timeout": 30,
    "execute_code_tools": [],
    "filesystem_allow_run": True,
    "filesystem_enabled": True,
    "filesystem_projects": [],
    "filesystem_run_requires_approval": True,
    "fleet_max_warm": 0,
    "fleet_port_base": 7870,
    "fleet_warm_grace_seconds": 0,
    "goal_enabled": True,
    "goal_eval_model": "",
    "goal_max_iterations": 8,
    "goal_monitor_interval": 60,
    "goal_no_progress_limit": 3,
    "goal_verify_timeout": 120.0,
    "identity_name": "protoagent",
    "identity_operator": "",
    "identity_org": "",
    "ingest_enabled": False,
    "ingest_tools": [],
    "instance_id": "",
    "knowledge_backend": "",
    "knowledge_db_path": "/sandbox/knowledge/agent.db",
    "knowledge_embedder": "",
    "knowledge_embeddings": True,
    "knowledge_facts": True,
    "knowledge_middleware": True,
    "knowledge_top_k": 5,
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
    "mcp_denylist": [],
    "mcp_enabled": False,
    "mcp_servers": [],
    "mcp_timeout_seconds": 20.0,
    "memory_middleware": True,
    "model_name": "protolabs/reasoning",
    "model_provider": "openai",
    "model_vision": False,
    "operator_allowed_dirs": [],
    "operator_mcp_enabled": False,
    "operator_mcp_tools": [],
    "plugins_dir": "",
    "plugins_disabled": [],
    "plugins_enabled": [],
    "plugins_sources_allow": [],
    "presence_penalty": None,
    "prompt_cache_enabled": True,
    "prompt_cache_force": False,
    "prompt_cache_ttl": "5m",
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
    "temperature": 0.2,
    "tools_deferred_enabled": False,
    "tools_deferred_keep": [],
    "tools_disabled": [],
    "top_k": -1,
    "top_p": None,
    "workflow_dir": "/sandbox/workflows",
}

# Fields handled by their own dedicated assertions, not the golden map.
_GOLDEN_EXEMPT = {"api_key", "auth_token", "plugin_config"}

# config_to_dict(from_yaml(example)) exact output — freezes the now-FIELDS-
# complete emitted surface (B1 PR-3) so a later change shows up as a reviewable
# diff. The diff vs the old (partial, 11-section) golden is purely ADDITIVE:
# the new sections agent_runtime / checkpoint / compaction / execute_code / goal
# / knowledge.embeddings+facts / middleware.enforcement / operator_mcp /
# prompt_cache / routing / telemetry appeared; no pre-existing value changed.
CONFIG_TO_DICT_GOLDEN = {
    "agent_runtime": "native",
    "auth": {
        "token": "",
    },
    "checkpoint": {
        "db_path": "/sandbox/checkpoints.db",
        "harvest_enabled": True,
        "keep_per_thread": 5,
        "max_age_days": 30,
        "prune_interval_hours": 6,
    },
    "commons": {
        "path": "",
    },
    "compaction": {
        "enabled": True,
        "keep_messages": 20,
        "model": "",
        "trigger": "fraction:0.8",
    },
    "discord": {
        "admin_ids": [],
        "bot_token": "",
        "enabled": False,
    },
    "execute_code": {
        "enabled": False,
        "timeout": 30,
    },
    "fleet": {
        "port_base": 7870,
        "discovery": {
            "port_min": 7860,
            "port_max": 7910,
            "mdns": True,
        },
        "warm": {
            "max": 0,
            "grace_seconds": 0,
        },
    },
    "goal": {
        "enabled": True,
        "eval_model": "",
        "max_iterations": 8,
    },
    "google": {
        "client_id": "",
        "client_secret": "",
        "enabled": False,
        "tz": "",
    },
    "identity": {
        "name": "protoagent",
        "operator": "",
        "org": "",
    },
    "knowledge": {
        "attach_inline_budget": 8000,
        "chunk_max_chars": 1200,
        "chunk_min_chars": 200,
        "chunk_overlap_chars": 150,
        "context_max_doc_chars": 12000,
        "contextual_enrichment": False,
        "db_path": "/sandbox/knowledge/agent.db",
        "embed_breaker_cooldown_s": 300.0,
        "embed_breaker_threshold": 2,
        "embed_model": "qwen3-embedding",
        "embeddings": True,
        "facts": True,
        "min_score": 0.0,
        "recall_preview_chars": 1000,
        "rrf_k": 60,
        "top_k": 5,
        "transcribe_model": "whisper-1",
        "vector_k": 20,
    },
    "mcp": {
        "denylist": [],
        "enabled": False,
        "servers": [],
        "timeout_seconds": 20.0,
    },
    "middleware": {
        "audit": True,
        "enforcement": False,
        "knowledge": True,
        "memory": True,
        "scheduler": True,
    },
    "model": {
        "api_base": "http://gateway:4000/v1",
        "api_key": "",
        "max_iterations": 50,
        "max_tokens": 32768,
        "name": "protolabs/reasoning",
        "provider": "openai",
        "temperature": 0.2,
        "vision": False,
    },
    "network": {
        "bind": "127.0.0.1",
    },
    "operator": {
        "allowed_dirs": [],
    },
    "operator_mcp": {
        "tools": [],
    },
    "plugins": {
        "dir": "",
        "disabled": [],
        "enabled": [],
        "sources": {
            "allow": [],
        },
    },
    "prompt_cache": {
        "enabled": True,
        "ttl": "5m",
        "warm": {
            "enabled": False,
            "interval_seconds": 3300,
        },
    },
    "routing": {
        "aux_model": "",
        "fallback_models": [],
    },
    "runtime": {
        "autostart_on_boot": False,
    },
    "skills": {
        "db_path": "/sandbox/skills.db",
        "dir": "",
        "enabled": True,
        "scope": "",
        "top_k": 5,
    },
    "subagents": {
        "researcher": {
            "enabled": True,
            "max_turns": 40,
            "model": "",
            "tools": [
                "current_time",
                "web_search",
                "fetch_url",
                "memory_recall",
                "memory_list",
            ],
        },
    },
    "telemetry": {
        "enabled": True,
        "retention_days": 90,
    },
}

# The dataclass attrs config_to_dict ACTUALLY emits, derived from the
# section/key structure of the golden dict. (a) maps each emitted golden
# leaf to the dataclass attr it feeds, (b) excludes attrs config_to_dict
# does NOT emit. The round-trip test asserts equality over exactly this set.
#
# discord/google are plugin sections -> they round-trip via plugin_config,
# checked separately. identity.org has no dataclass attr (getattr default).
EMITTED_ATTRS = {
    # model.*
    "model_provider",
    "model_name",
    "api_base",
    "api_key",  # redacted -> resolves to ""
    "temperature",
    "max_tokens",
    "model_vision",
    "max_iterations",
    # subagents.researcher
    "researcher",
    # middleware.*
    "knowledge_middleware",
    "audit_middleware",
    "memory_middleware",
    "scheduler_enabled",
    # knowledge.*
    "knowledge_db_path",
    "embed_model",
    "transcribe_model",
    "knowledge_top_k",
    "knowledge_vector_k",
    "knowledge_rrf_k",
    "knowledge_min_score",
    "knowledge_recall_preview_chars",
    "knowledge_embed_breaker_threshold",
    "knowledge_embed_breaker_cooldown_s",
    "knowledge_chunk_max_chars",
    "knowledge_chunk_overlap_chars",
    "knowledge_chunk_min_chars",
    "knowledge_contextual_enrichment",
    "knowledge_context_max_doc_chars",
    "knowledge_attach_inline_budget",
    # skills.*
    "skills_enabled",
    "skills_db_path",
    "skills_top_k",
    "skills_dir",
    # mcp.*
    "mcp_enabled",
    "mcp_servers",
    "mcp_timeout_seconds",
    "mcp_denylist",
    # plugins.* (disabled + sources.allow added by the 2026-06-10 N6 fix —
    # config_to_dict used to emit only enabled/dir, so any complete-dict
    # consumer lost them)
    "plugins_enabled",
    "plugins_disabled",
    "plugins_dir",
    "plugins_sources_allow",
    # identity.*
    "identity_name",
    "identity_operator",
    # auth.token
    "auth_token",  # redacted -> resolves to ""
    # runtime.*
    "autostart_on_boot",
    # operator.*
    "operator_allowed_dirs",
    # --- B1 PR-3: config_to_dict is now FIELDS-complete, so these 27
    # newly-emitted keys (derived key->attr from FIELDS) must also round-trip. ---
    # agent_runtime
    "agent_runtime",
    # operator_mcp.tools
    "operator_mcp_tools",
    # routing.*
    "aux_model",
    "routing_fallback_models",
    # compaction.*
    "compaction_enabled",
    "compaction_trigger",
    "compaction_keep_messages",
    "compaction_model",
    # goal.*
    "goal_enabled",
    "goal_max_iterations",
    "goal_eval_model",
    # execute_code.*
    "execute_code_enabled",
    "execute_code_timeout",
    # prompt_cache.* (incl. prompt_cache.warm.*)
    "prompt_cache_enabled",
    "prompt_cache_ttl",
    "cache_warming_enabled",
    "cache_warming_interval_seconds",
    # knowledge.embeddings / knowledge.facts
    "knowledge_embeddings",
    "knowledge_facts",
    # checkpoint.*
    "checkpoint_db_path",
    "checkpoint_keep_per_thread",
    "checkpoint_max_age_days",
    "checkpoint_prune_interval_hours",
    "checkpoint_harvest_enabled",
    # middleware.enforcement
    "enforcement_enabled",
    # telemetry.*
    "telemetry_enabled",
    "telemetry_retention_days",
    # network.bind / fleet.* (box runtime, ADR 0047 D8)
    "bind_host",
    "fleet_port_base",
    "discovery_port_min",
    "discovery_port_max",
    "discovery_mdns",
    "fleet_max_warm",
    "fleet_warm_grace_seconds",
}


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
# (b) config_to_dict golden (frozen partial output)
# ---------------------------------------------------------------------------


def test_config_to_dict_golden():
    cfg = LangGraphConfig.from_yaml(EXAMPLE_PATH)
    assert config_to_dict(cfg) == CONFIG_TO_DICT_GOLDEN


# ---------------------------------------------------------------------------
# (c) Round-trip over exactly the attrs config_to_dict emits
# ---------------------------------------------------------------------------


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

    # discord/google are plugin sections — they round-trip via plugin_config.
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
          ingest: true
        enforcement:
          disallowed_tools:
            - dangerous_tool
          rate_limits:
            web_search: 5
        ingest:
          tools:
            - memory_ingest
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.enforcement_enabled is True
    assert cfg.enforcement_disallowed_tools == ["dangerous_tool"]
    assert cfg.enforcement_rate_limits == {"web_search": 5}
    assert cfg.ingest_enabled is True
    assert cfg.ingest_tools == ["memory_ingest"]


def test_case6b_middleware_flags_only_no_toplevel_sections(tmp_path):
    """Flags read True from middleware.*; lists default to []/{} because their
    OWN top-level sections are absent (proves the split)."""
    path = _write_yaml(
        tmp_path,
        """
        middleware:
          enforcement: true
          ingest: true
        """,
    )
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.enforcement_enabled is True
    assert cfg.enforcement_disallowed_tools == []
    assert cfg.enforcement_rate_limits == {}
    assert cfg.ingest_enabled is True
    assert cfg.ingest_tools == []


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
    d1 = tmp_path / "truthy"; d1.mkdir()
    d0 = tmp_path / "falsy"; d0.mkdir()
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


def test_plugins_disabled_and_sources_allow_survive_config_to_dict():
    """N6 (2026-06-10 prod-readiness audit): config_to_dict emitted only
    plugins.{enabled, dir}, dropping `disabled` + `sources.allow`. The YAML file
    round-trip was never lossy (apply_updates_to_yaml merges in place and never
    deletes absent keys) — the DICT was: any consumer treating it as the complete
    config lost them, and the Settings UI could never display/edit them. This
    pins the full plugins.* section round-tripping through from_dict ->
    config_to_dict with NON-default values."""
    cfg = LangGraphConfig.from_dict({
        "plugins": {
            "enabled": ["alpha"],
            "disabled": ["beta"],
            "dir": "/custom/plugins",
            "sources": {"allow": ["github.com/protolabsai/*"]},
        },
    })
    d = config_to_dict(cfg)
    assert d["plugins"] == {
        "enabled": ["alpha"],
        "disabled": ["beta"],
        "dir": "/custom/plugins",
        "sources": {"allow": ["github.com/protolabsai/*"]},
    }
