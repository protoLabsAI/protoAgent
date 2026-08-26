"""Provider shaping follows each concrete call, never the graph default (#3156)."""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from graph.agent import ObservableModelFallbackMiddleware
from graph.middleware.model_override import ModelOverrideMiddleware
from graph.middleware.provider_shape import ProviderShapeMiddleware
from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX
from graph.providers.identity import model_provider_type, tag_model_provider


class _Model:
    def __init__(self, name: str):
        self.model_name = name


class _Request:
    def __init__(self, model, *, state=None, system_message=None, model_settings=None):
        self.model = model
        self.state = state or {}
        self.system_message = system_message
        self.model_settings = model_settings or {}

    def override(self, **changes):
        values = {
            "model": self.model,
            "state": self.state,
            "system_message": self.system_message,
            "model_settings": self.model_settings,
            **changes,
        }
        return _Request(**values)


def _model(name: str, provider_type: str):
    return tag_model_provider(_Model(name), provider_type, provider_type)


def test_dispatcher_is_stateless_across_gateway_codex_and_claude_calls():
    """One compiled graph can alternate lanes without retaining the last shape."""
    shape = ProviderShapeMiddleware()
    system = SystemMessage(content="You are Aria.")

    gateway = shape._transform(_Request(_model("same-name", "openai-compat"), system_message=system))
    codex = shape._transform(_Request(_model("same-name", "openai-codex"), system_message=system))
    claude = shape._transform(_Request(_model("same-name", "anthropic-oauth"), system_message=system))
    gateway_again = shape._transform(_Request(_model("same-name", "openai-compat"), system_message=system))

    assert gateway.system_message is system and gateway.model_settings == {}
    assert codex.system_message is None
    assert codex.model_settings["instructions"] == "You are Aria."
    assert claude.system_message.content[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
    assert gateway_again.system_message is system and gateway_again.model_settings == {}


def test_gateway_graph_chat_override_to_codex_shapes_the_selected_call(monkeypatch):
    """Regression: the exact Gateway → Codex composer switch that returned 400."""
    built = _model("gpt-5.6-sol", "openai-codex")
    monkeypatch.setattr("graph.llm.create_llm", lambda *_a, **_kw: built)
    override = ModelOverrideMiddleware(config=object())
    shape = ProviderShapeMiddleware()
    request = _Request(
        _model("protolabs/reasoning", "openai-compat"),
        state={"model": "openai-codex:gpt-5.6-sol"},
        system_message=SystemMessage(content="FINAL COMPOSED PROMPT"),
    )
    seen = {}

    override.wrap_model_call(request, lambda selected: shape.wrap_model_call(selected, lambda wire: seen.update(wire=wire)))

    wire = seen["wire"]
    assert wire.model is built
    assert model_provider_type(wire.model) == "openai-codex"
    assert wire.system_message is None
    assert wire.model_settings["instructions"] == "FINAL COMPOSED PROMPT"


class _CaptureToolModel(GenericFakeChatModel):
    """A real BaseChatModel boundary that records factory binds and input messages."""

    bound_calls: list[dict] = []
    input_calls: list[list] = []

    def bind_tools(self, _tools, **kwargs):
        self.bound_calls.append(dict(kwargs))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.input_calls.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.asyncio
async def test_real_graph_gateway_to_codex_reaches_model_without_system_role(monkeypatch):
    """Exercise create_agent's actual middleware composition and bind boundary."""
    from langgraph.checkpoint.memory import MemorySaver

    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig

    gateway = tag_model_provider(
        _CaptureToolModel(messages=iter([AIMessage(content="unused")])),
        "openai-compat",
        "gateway",
    )
    codex = tag_model_provider(
        _CaptureToolModel(messages=iter([AIMessage(content="ok")])),
        "openai-codex",
        "openai-codex",
    )
    monkeypatch.setattr("graph.agent.create_llm", lambda *_a, **_kw: gateway)
    monkeypatch.setattr("graph.llm.create_llm", lambda *_a, **_kw: codex)

    graph = create_agent_graph(
        LangGraphConfig(prompt_capture_enabled=False),
        include_subagents=False,
        checkpointer=MemorySaver(),
    )
    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="hi")],
            "session_id": "provider-isolation",
            "model": "openai-codex:gpt-5.6-sol",
        },
        config={"configurable": {"thread_id": "provider-isolation"}},
    )

    assert codex.bound_calls
    assert codex.bound_calls[-1]["instructions"]
    assert codex.input_calls
    assert not any(isinstance(message, SystemMessage) for message in codex.input_calls[-1])


def test_codex_graph_chat_override_to_gateway_does_not_apply_codex_shape(monkeypatch):
    """The inverse switch must not strand a Gateway prompt in instructions."""
    built = _model("protolabs/reasoning", "openai-compat")
    monkeypatch.setattr("graph.llm.create_llm", lambda *_a, **_kw: built)
    override = ModelOverrideMiddleware(config=object())
    shape = ProviderShapeMiddleware()
    system = SystemMessage(content="FINAL COMPOSED PROMPT")
    request = _Request(
        _model("gpt-5.6-sol", "openai-codex"),
        state={"model": "gateway:protolabs/reasoning"},
        system_message=system,
    )
    seen = {}

    override.wrap_model_call(request, lambda selected: shape.wrap_model_call(selected, lambda wire: seen.update(wire=wire)))

    wire = seen["wire"]
    assert model_provider_type(wire.model) == "openai-compat"
    assert wire.system_message is system
    assert wire.model_settings == {}


def test_fallback_attempt_is_reshaped_for_its_own_provider():
    """A Gateway primary and Codex fallback get separate per-attempt wire shapes."""
    primary = _model("primary", "openai-compat")
    fallback = _model("fallback", "openai-codex")
    routing = ObservableModelFallbackMiddleware(fallback)
    shape = ProviderShapeMiddleware()
    attempts = []

    def invoke(request):
        def wire_call(wire):
            attempts.append(wire)
            if wire.model is primary:
                raise RuntimeError("primary unavailable")
            return "ok"

        return shape.wrap_model_call(request, wire_call)

    result = routing.wrap_model_call(
        _Request(primary, system_message=SystemMessage(content="PROMPT")),
        invoke,
    )

    assert result == "ok"
    assert attempts[0].system_message is not None and attempts[0].model_settings == {}
    assert attempts[1].model is fallback and attempts[1].system_message is None
    assert attempts[1].model_settings["instructions"] == "PROMPT"


def test_provider_identity_survives_a_langchain_style_binding():
    raw = _model("gpt", "openai-codex")

    class _Binding:
        bound = raw

    assert model_provider_type(_Binding()) == "openai-codex"


def test_untagged_external_delegate_or_plugin_model_is_never_inferred_from_its_name():
    """Delegate adapters own their wire protocol; lead shaping cannot cross that seam."""
    system = SystemMessage(content="EXTERNAL SYSTEM")
    request = _Request(_Model("gpt-5-codex"), system_message=system)

    wire = ProviderShapeMiddleware()._transform(request)

    assert wire is request
    assert wire.system_message is system
    assert wire.model_settings == {}
