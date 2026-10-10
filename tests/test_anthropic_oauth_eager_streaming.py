"""The anthropic-oauth client turns on ``eager_input_streaming`` for ``stream_args`` tools.

ADR 0118 D3 streams a write tool's ``code`` argument as a live inline preview. Anthropic
only emits ``input_json_delta`` eagerly when the tool carries ``eager_input_streaming:
true`` on the request — without it the whole argument arrives in one burst at message end
and the preview never streams (#4120). ``_OAuthChatAnthropic`` must set that flag on
exactly the tools whose source tool declared ``stream_args`` in its metadata, resolved by
name from the bound set (never a hard-coded tool list), and leave plain tools alone.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

import graph.providers.anthropic_oauth as ao


@tool
def show_artifact(code: str) -> str:
    """A write tool that streams its ``code`` argument."""
    return code


show_artifact.metadata = {"stream_args": "code"}


@tool
def get_weather(city: str) -> str:
    """A plain tool with no stream_args metadata."""
    return city


@pytest.fixture
def oauth_model():
    if ao._OAuthChatAnthropic is None:  # pragma: no cover — langchain-anthropic absent
        pytest.skip("langchain-anthropic not installed")
    return ao._OAuthChatAnthropic(
        model="claude-sonnet-5-5",
        oauth_token="cc-TEST",
        api_key="oauth-via-auth-token",
        max_tokens=64,
        max_retries=0,
    )


def _payload_tools(model) -> dict[str, dict]:
    """Bind one stream_args tool + one plain tool, build the request payload, index by name."""
    bound = model.bind_tools([show_artifact, get_weather])
    payload = model._get_request_payload([HumanMessage("hi")], **bound.kwargs)
    return {t["name"]: t for t in payload["tools"]}


def test_eager_input_streaming_only_on_stream_args_tool(oauth_model):
    tools = _payload_tools(oauth_model)
    assert tools["show_artifact"]["eager_input_streaming"] is True
    # The plain tool is left untouched — the flag is absent, not merely False.
    assert "eager_input_streaming" not in tools["get_weather"]


def test_bind_tools_records_only_stream_args_names(oauth_model):
    oauth_model.bind_tools([show_artifact, get_weather])
    assert oauth_model._stream_args_tool_names == {"show_artifact"}


def test_no_eager_flag_when_no_tools_declare_stream_args(oauth_model):
    bound = oauth_model.bind_tools([get_weather])
    payload = oauth_model._get_request_payload([HumanMessage("hi")], **bound.kwargs)
    assert all("eager_input_streaming" not in t for t in payload["tools"])


def test_collect_stream_args_names_ignores_non_tools():
    # A bare callable/dict with no .metadata must not crash or be counted.
    assert ao._stream_args_tool_names([show_artifact, get_weather, {"name": "raw"}, lambda x: x]) == {
        "show_artifact"
    }
