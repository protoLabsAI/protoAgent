"""The agent can read its own effective config — without reading its own secrets (#2540)."""

from __future__ import annotations

import json

import pytest

from graph.config import LangGraphConfig
from tools.config_tools import _REDACTED, build_config_tools, redact_for_agent


@pytest.fixture
def show_config():
    cfg = LangGraphConfig()
    cfg.mcp_servers = [
        {
            "name": "gh",
            "transport": "stdio",
            "command": "gh-mcp",
            "env": {"GITHUB_TOKEN": "ghp_realtoken", "SOME_FLAG": "1"},
        },
        {
            "name": "remote",
            "transport": "http",
            "url": "https://mcp.example.com",
            "headers": {"Authorization": "Bearer sk-live-abc"},
        },
    ]
    cfg.plugin_config = {"project_board": {"repo": "protoLabsAI/protoAgent", "api_token": "pbt_live"}}
    return build_config_tools(cfg)[0]


def test_agent_can_see_how_its_plugin_is_bound(show_config):
    """The #2540 incident: two sessions lost to a board bound to the wrong repo, with
    the answer in a file no tool could open."""
    out = show_config.invoke({"section": "project_board"})

    assert "protoLabsAI/protoAgent" in out
    assert json.loads(out)["project_board"]["repo"] == "protoLabsAI/protoAgent"


def test_mcp_env_and_headers_never_reach_the_model(show_config):
    """`config_to_dict` emits these verbatim — correct for the token-gated operator API,
    wrong for a destination that is the model's context and the chat transcript."""
    out = show_config.invoke({"section": "mcp"})

    assert "ghp_realtoken" not in out
    assert "sk-live-abc" not in out
    servers = {s["name"]: s for s in json.loads(out)["mcp"]["servers"]}
    # Masked, not dropped — "a token IS set here" is exactly what a diagnosing agent needs.
    assert servers["gh"]["env"]["GITHUB_TOKEN"] == _REDACTED
    assert servers["gh"]["env"]["SOME_FLAG"] == _REDACTED
    assert servers["remote"]["headers"]["Authorization"] == _REDACTED
    # Non-secret wiring survives, or the tool would be useless for diagnosis.
    assert servers["gh"]["command"] == "gh-mcp"
    assert servers["remote"]["url"] == "https://mcp.example.com"


def test_no_secret_survives_anywhere_in_the_whole_document(show_config):
    """The blanket check: whatever section it sits in, a credential must not come back."""
    out = show_config.invoke({})

    for leaked in ("ghp_realtoken", "sk-live-abc", "pbt_live"):
        assert leaked not in out, leaked


def test_redaction_covers_sections_nobody_updated_this_module_for():
    """Fails closed by SHAPE, so a plugin section added later is covered without anyone
    remembering to come back here."""
    doc = redact_for_agent(
        {
            "some_future_plugin": {
                "endpoint": "https://api.example.com",
                "access_token": "tok_live_123",
                "nested": [{"client_secret": "cs_live_456"}],
                "env": {"ANYTHING": "could-be-a-token"},
            }
        }
    )

    section = doc["some_future_plugin"]
    assert section["endpoint"] == "https://api.example.com"
    assert section["access_token"] == _REDACTED
    assert section["nested"][0]["client_secret"] == _REDACTED
    assert section["env"]["ANYTHING"] == _REDACTED


def test_blank_values_are_left_alone_so_unset_still_reads_as_unset():
    """Masking a blank would say "a token is set here" when none is — the exact
    confusion this tool exists to end."""
    doc = redact_for_agent({"svc": {"api_key": "", "token": None, "env": {"EMPTY": ""}}})

    assert doc["svc"]["api_key"] == ""
    assert doc["svc"]["token"] is None
    assert doc["svc"]["env"]["EMPTY"] == ""


def test_unknown_section_suggests_instead_of_failing_blind(show_config):
    out = show_config.invoke({"section": "project_bord"})

    assert out.startswith("Error:")
    assert "project_board" in out  # near-match suggestion
    assert "Sections:" in out


def test_tool_description_tells_the_model_what_the_marker_means(show_config):
    """A masked value is only useful if the model knows a mask is what it's seeing."""
    assert _REDACTED in show_config.description
    assert "Read-only" in show_config.description


def test_show_config_binds_with_the_core_toolset():
    from tools.lg_tools import get_all_tools

    names = {t.name for t in get_all_tools(graph_config=LangGraphConfig())}
    assert "show_config" in names

    # No config → nothing to introspect, and nothing to leak.
    assert "show_config" not in {t.name for t in get_all_tools()}


def test_operator_can_turn_it_off():
    """ADR 0005: the operator denylist covers it like any other core tool."""
    from tools.lg_tools import get_all_tools, set_disabled_tools

    try:
        set_disabled_tools(["show_config"])
        names = {t.name for t in get_all_tools(graph_config=LangGraphConfig())}
        assert "show_config" not in names
    finally:
        set_disabled_tools([])
