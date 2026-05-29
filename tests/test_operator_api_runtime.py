from __future__ import annotations

from graph.config import LangGraphConfig, SubagentDef
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
        knowledge_store=_Store(),
        scheduler=_Scheduler(),
        cache_warmer=object(),
        goal_controller=object(),
    )

    assert status["model"]["name"] == "protolabs/reasoning"
    assert status["model"]["api_key_configured"] is True
    assert status["model"]["api_base"] == "https://api.proto-labs.ai/v1"
    assert status["project"]["path"] == "/tmp/protoagent"
    assert status["knowledge"]["resolved_path"] == "/tmp/protoagent/knowledge.db"
    assert status["scheduler"]["backend"] == "local"
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
