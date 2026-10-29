from __future__ import annotations

from graph.config import LangGraphConfig, Provider, SubagentDef, model_is_configured
from operator_api.runtime import build_runtime_status
from operator_api.subagents import list_subagents


class _Store:
    path = "/tmp/protoagent/knowledge.db"


class _Scheduler:
    name = "local"


def test_runtime_status_redacts_secret_values() -> None:
    cfg = LangGraphConfig(
        model_name="protolabs/reasoning",
        api_base="https://api.proto-labs.ai/v1",
        api_key="sk-secret",
        identity_name="protoagent",
        identity_operator="kj",
    )

    status = build_runtime_status(
        config=cfg,
        setup_complete=True,
        graph_loaded=True,
        project_path="/tmp/protoagent",
        allowed_dirs=["/tmp/protoagent", "/home/kj/projects/foo"],
        knowledge_store=_Store(),
        scheduler=_Scheduler(),
        cache_warmer=object(),
    )

    assert status["model"]["name"] == "protolabs/reasoning"
    assert status["model"]["api_key_configured"] is True
    assert status["model"]["api_base"] == "https://api.proto-labs.ai/v1"
    assert status["project"]["path"] == "/tmp/protoagent"
    assert status["project"]["allowed_dirs"] == ["/tmp/protoagent", "/home/kj/projects/foo"]
    assert status["knowledge"]["resolved_path"] == "/tmp/protoagent/knowledge.db"
    # Flag on (default) + store built → ready.
    assert status["knowledge"]["status"] == "ready"
    assert status["scheduler"]["backend"] == "local"
    assert status["agent_runtime"] == "native"  # no agent_runtime set → native
    assert "sk-secret" not in repr(status)


def test_runtime_status_handles_missing_config() -> None:
    status = build_runtime_status(
        config=None,
        setup_complete=False,
        graph_loaded=False,
    )

    assert status["setup_complete"] is False
    assert status["graph_loaded"] is False
    assert status["model"] is None
    assert status["knowledge"]["enabled"] is False
    assert status["agent_runtime"] == "native"  # no config → default native
    # No config yet (booting) → initializing, not "disabled".
    assert status["knowledge"]["status"] == "initializing"
    assert status["project"]["allowed_dirs"] == []
    assert status["version"] == ""  # not passed — defaults empty, key still present


def test_runtime_status_carries_version() -> None:
    # The console↔server /api/* surface has no other versioning (hub↔remote skew,
    # ADR 0042 §I) — runtime status carries the app version in both shapes.
    s = build_runtime_status(config=None, setup_complete=False, graph_loaded=False, version="0.32.0")
    assert s["version"] == "0.32.0"
    s2 = build_runtime_status(config=LangGraphConfig(), setup_complete=True, graph_loaded=True, version="0.32.0")
    assert s2["version"] == "0.32.0"


def test_model_is_configured_distinguishes_explore_mode(monkeypatch) -> None:
    # "Explore without a model" (#4566). `resolve_model_route` consults OPENAI_API_KEY, so
    # clear it — a key leaking in from the shell would read explore mode as configured.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert model_is_configured(None) is False
    # Dataclass defaults only — a gateway base URL and a blank key — are explore mode.
    assert model_is_configured(LangGraphConfig()) is False
    # A legacy/default-route key (or the env key) means a usable model.
    assert model_is_configured(LangGraphConfig(api_key="sk-live")) is True
    # A DECLARED connection (ADR 0106) with an endpoint, even keyless (a local vLLM).
    declared = LangGraphConfig(
        providers=[Provider(id="local", base_url="http://127.0.0.1:8000/v1")],
        providers_declared=True,
    )
    assert model_is_configured(declared) is True
    # ...but a declared-EMPTY list (the operator removed the last connection) is not.
    assert model_is_configured(LangGraphConfig(providers=[], providers_declared=True)) is False
    # A native OAuth subscription authenticates from a credential store, not a key.
    assert model_is_configured(LangGraphConfig(model_provider="anthropic-oauth")) is True
    # An ACP coding-agent runtime is its own brain — no gateway key needed.
    assert model_is_configured(LangGraphConfig(agent_runtime="acp:claude")) is True


def test_runtime_payload_reports_model_configured() -> None:
    # Additive field, present in BOTH payload shapes, defaulting False, and carrying the
    # caller's value verbatim — the console recomputes it each poll, so saving a model
    # flips it with no restart (#4566).
    assert build_runtime_status(config=None, setup_complete=True, graph_loaded=False)["model_configured"] is False

    explore = build_runtime_status(
        config=LangGraphConfig(), setup_complete=True, graph_loaded=False, model_configured=False
    )
    assert explore["model_configured"] is False

    configured = build_runtime_status(
        config=LangGraphConfig(api_key="sk-live"), setup_complete=True, graph_loaded=True, model_configured=True
    )
    assert configured["model_configured"] is True

    # Pre-setup shape (config is None) carries the field too.
    pre_setup = build_runtime_status(config=None, setup_complete=False, graph_loaded=False, model_configured=True)
    assert pre_setup["model_configured"] is True


def test_list_subagents_uses_registry_and_config_override() -> None:
    cfg = LangGraphConfig()
    cfg.researcher = SubagentDef(
        enabled=False,
        tools=["current_time"],
        max_turns=7,
    )

    subagents = list_subagents(cfg)
    researcher = next(item for item in subagents if item["name"] == "researcher")

    assert researcher["enabled"] is False
    assert researcher["tools"] == ["current_time"]
    assert researcher["max_turns"] == 7
    assert "web_search" in researcher["default_tools"]
