"""protoAgent — FastAPI server wrapping a LangGraph agent with A2A.

This is the main entry point. It:

1. Initializes LangGraph (``graph/agent.py``) + the LiteLLM gateway
   connection via ``graph/llm.py``.
2. Mounts the full A2A surface (``a2a_handler.register_a2a_routes``)
   — JSON-RPC on ``POST /a2a``, SSE streaming, push notifications,
   ``tasks/*`` CRUD, agent card at ``/.well-known/agent.json``.
3. Mounts an OpenAI-compatible chat-completions endpoint so the agent
   can be registered as a model in the LiteLLM gateway / OpenWebUI.
4. Optionally mounts a Gradio chat UI for direct operator access.
5. Exposes a Prometheus ``/metrics`` endpoint when the ``metrics``
   module is active.

### Forking checklist

- Change the agent identity in ``_build_agent_card`` (name, description,
  skills, extensions).
- Drop ``SOUL.md`` in the workspace to override the default agent prompt.
- Add your real tools to ``tools/lg_tools.py`` and wire them into
  ``graph/subagents/config.py`` if you want specialized delegation.
- Set the ``<AGENT>_API_KEY`` env var name below to match your agent's
  auth naming convention.
"""

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from graph.output_format import extract_output

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Root-level log config. Python's default is WARNING, which silently filters
# every `logger.info(...)` call — including "webhook delivered" lines from
# a2a_handler, making the A2A/webhook path invisible in docker logs.
# LOG_LEVEL env var lets operators tune without a code change.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("protoagent.server")


# ---------------------------------------------------------------------------
# Agent setup
# ---------------------------------------------------------------------------

_graph = None          # LangGraph compiled graph
_graph_config = None   # LangGraphConfig
_checkpointer = None   # MemorySaver for session persistence


def _init_langgraph_agent():
    """Initialize the LangGraph agent backend."""
    global _graph, _graph_config, _checkpointer

    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    config_path = Path(__file__).parent / "config" / "langgraph-config.yaml"
    _graph_config = LangGraphConfig.from_yaml(config_path)
    _checkpointer = MemorySaver()
    _graph = create_agent_graph(_graph_config)
    log.info("LangGraph agent initialized (model: %s)", _graph_config.model_name)


# ---------------------------------------------------------------------------
# Chat backend — called by the A2A handler + OpenAI-compat endpoint
# ---------------------------------------------------------------------------

async def chat(message: str, session_id: str) -> list[dict[str, Any]]:
    """Route a user message through LangGraph and return the final assistant
    response as a list of ``{"role": "assistant", "content": ...}`` dicts.

    This is the non-streaming entry point used by Gradio + the OpenAI-compat
    endpoint. The A2A handler uses ``_chat_langgraph_stream`` instead to
    capture tool events and emit the cost-v1 DataPart on the terminal
    artifact.
    """
    return await _chat_langgraph(message, session_id)


async def _chat_langgraph_stream(
    message: str,
    session_id: str,
    *,
    caller_trace: dict | None = None,
):
    """Async generator — yields (event_type, payload) tuples from the
    LangGraph run. Consumed by ``a2a_handler.register_a2a_routes`` to
    drive the background task runner + SSE streaming.

    Event contract (matches what the A2A handler expects):

    - ``tool_start`` / ``tool_end`` — status frames w/ tool name + preview
    - ``usage`` — per-LLM-call token usage for the cost-v1 DataPart
    - ``done`` — terminal; payload is the final user-facing text
    - ``error`` — terminal; payload is the error string

    ``caller_trace`` is the ``a2a.trace`` metadata from the incoming
    A2A message. When present, Langfuse stamps ``caller_trace_id`` +
    ``caller_span_id`` so operators can cross-reference this trace to
    the dispatching agent's trace in the same project.
    """
    import tracing
    from langchain_core.messages import HumanMessage

    trace_meta: dict = {"message_preview": message[:100]}
    if caller_trace:
        if caller_trace.get("traceId"):
            trace_meta["caller_trace_id"] = caller_trace["traceId"]
        if caller_trace.get("spanId"):
            trace_meta["caller_span_id"] = caller_trace["spanId"]

    async with tracing.trace_session(
        session_id=session_id,
        name="a2a-stream",
        metadata=trace_meta,
    ):
        try:
            # thread_id prefix isolates A2A sessions from Gradio chat in the
            # shared MemorySaver checkpointer.
            config = {
                "configurable": {"thread_id": f"a2a:{session_id}"},
                "recursion_limit": 200,
            }
            if _checkpointer:
                config["checkpointer"] = _checkpointer

            # Accumulate model tokens silently — chunk-boundary tag splitting
            # on the <scratch_pad>/<output> protocol was a state-machine
            # rabbit hole; A2A consumers already get useful progress signal
            # from tool_start/tool_end. Final text is extracted cleanly once
            # on the `done` frame via extract_output().
            accumulated_raw = ""

            async for event in _graph.astream_events(
                {"messages": [HumanMessage(content=message)], "session_id": session_id},
                config=config,
                version="v2",
            ):
                kind = event.get("event", "")
                name = event.get("name", "")

                if kind == "on_tool_start":
                    tool_input = event.get("data", {}).get("input", "")
                    preview = str(tool_input)[:200] if tool_input else ""
                    yield ("tool_start", f"🔧 {name}: {preview}")

                elif kind == "on_tool_end":
                    output = event.get("data", {}).get("output", "")
                    preview = str(output)[:300] if output else ""
                    yield ("tool_end", f"✅ {name} → {preview}")
                    # If your fork declares effect-domain-v1 on the agent card,
                    # this is where you map successful tool calls to the
                    # matching worldstate-delta-v1 DataPart entry. The template
                    # ships with no declared effects — skip the yield.

                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        raw = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                        accumulated_raw += raw

                elif kind == "on_chat_model_end":
                    # Capture per-call token usage for the cost-v1 DataPart
                    # the A2A handler emits on the terminal artifact.
                    # LangChain exposes normalized usage on
                    # `output.usage_metadata` — requires `stream_usage=True`
                    # on the ChatOpenAI client (see graph/llm.py). Without
                    # that flag usage_metadata is None on AIMessageChunks.
                    output = event.get("data", {}).get("output")
                    usage = getattr(output, "usage_metadata", None) if output else None
                    if usage:
                        yield ("usage", {
                            "input_tokens": int(usage.get("input_tokens", 0) or 0),
                            "output_tokens": int(usage.get("output_tokens", 0) or 0),
                        })

            yield ("done", extract_output(accumulated_raw))

        except GeneratorExit:
            # Expected: A2A consumers (e.g. Workstacean's A2AExecutor) break
            # out of the SSE loop after capturing the initial task event,
            # then hand off to TaskTracker for polling. Re-raise so Python
            # finalizes the generator cleanly; the OTel cross-context detach
            # noise this used to emit is silenced at the logger level in
            # tracing.py.
            raise
        except Exception as e:
            log.exception(
                "[a2a-stream] unhandled exception for session=%s: %s",
                session_id, e,
            )
            yield ("error", str(e))
        finally:
            tracing.flush()


async def _chat_langgraph(message: str, session_id: str) -> list[dict[str, Any]]:
    """Non-streaming LangGraph entry — used by Gradio + OpenAI-compat."""
    import tracing
    from langchain_core.messages import HumanMessage, AIMessage

    async with tracing.trace_session(
        session_id=session_id,
        name="chat",
        metadata={"message_preview": message[:100]},
    ):
        try:
            config = {"configurable": {"thread_id": f"gradio:{session_id}"}}
            if _checkpointer:
                config["checkpointer"] = _checkpointer

            result = await _graph.ainvoke(
                {"messages": [HumanMessage(content=message)], "session_id": session_id},
                config=config,
            )

            messages = result.get("messages", [])
            response = ""
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) and msg.content:
                    response = msg.content if isinstance(msg.content, str) else str(msg.content)
                    break

            response = extract_output(response)
            return [{"role": "assistant", "content": response}]
        except Exception as e:
            log.exception(
                "[chat] unhandled exception for session=%s: %s",
                session_id, e,
            )
            return [{"role": "assistant", "content": f"**Error:** {e}"}]
        finally:
            tracing.flush()


# ---------------------------------------------------------------------------
# Agent card — EDIT THIS when forking
# ---------------------------------------------------------------------------

AGENT_NAME = os.environ.get("AGENT_NAME", "protoagent")


def _build_security_schemes() -> dict:
    """Return securitySchemes dict, adding bearer only when A2A_AUTH_TOKEN is set."""
    schemes: dict = {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}
    if os.environ.get("A2A_AUTH_TOKEN", ""):
        schemes["bearer"] = {"type": "http", "scheme": "bearer"}
    return schemes


def _build_agent_card(host: str) -> dict:
    """Build the A2A agent card served at /.well-known/agent-card.json.

    **Fork this.** Replace ``name``, ``description``, ``skills``, and
    any extensions with your agent's actual surface. Keep the
    ``capabilities`` block as-is unless you have a reason to turn off
    streaming or push notifications — the A2A handler supports both
    and consumers rely on the flags being honest.

    Extension declarations:

    - ``effect-domain-v1`` — declare per-skill world-state mutations
      so Workstacean's L1 planner can rank your agent against
      goals that target those state selectors. Only declare effects
      that actually mutate shared state.
    - ``cost-v1`` — declare that your agent emits a cost-v1 DataPart
      on every terminal task. This template DOES emit it automatically
      (see the ``on_chat_model_end`` handler in
      ``_chat_langgraph_stream``), so the declaration is kept — drop
      it only if you strip the usage-capture.
    """
    return {
        "name": AGENT_NAME,
        "description": (
            "protoAgent template — A2A-compliant LangGraph agent. "
            "Replace this description with your agent's actual purpose."
        ),
        # A2A spec: the url field must point at the JSON-RPC endpoint
        # (where message/send is accepted), NOT the server root.
        "url": f"http://{host}/a2a",
        "version": "0.1.0",
        "provider": {
            "organization": "protoLabsAI",
            "url": "https://github.com/protoLabsAI",
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": False,
            "extensions": [
                # cost-v1 emission is wired by default via the `on_chat_model_end`
                # capture in _chat_langgraph_stream above.
                {"uri": "https://protolabs.ai/a2a/ext/cost-v1"},
            ],
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/markdown"],
        "skills": [
            # REPLACE — template ships one placeholder skill.
            {
                "id": "chat",
                "name": "Chat",
                "description": "General-purpose chat interface. Replace with your agent's real skills.",
                "tags": ["template"],
                "examples": ["hello", "what can you do?"],
            },
        ],
        "securitySchemes": _build_security_schemes(),
        "security": [{"apiKey": []}],
    }


# ---------------------------------------------------------------------------
# Main — FastAPI + Gradio + A2A + OpenAI-compat + Prometheus
# ---------------------------------------------------------------------------

def _main():
    parser = argparse.ArgumentParser(description=f"{AGENT_NAME} — protoAgent server")
    parser.add_argument("--port", type=int, default=7870)
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    # Initialize observability
    import tracing
    import metrics
    tracing.init()
    metrics.init()

    _init_langgraph_agent()

    # Optional Gradio chat UI — comment out if your agent is headless.
    from chat_ui import create_chat_app
    blocks = create_chat_app(
        chat_fn=chat,
        title=AGENT_NAME,
        subtitle="protoAgent",
        placeholder="Send a message...",
        pwa=True,
    )

    import gradio as gr
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel as PydanticBaseModel

    fastapi_app = FastAPI(title=f"{AGENT_NAME} — protoAgent")

    # --- Chat API -----------------------------------------------------------
    class ChatRequest(PydanticBaseModel):
        message: str
        session_id: str = "api-default"

    @fastapi_app.post("/api/chat")
    async def _api_chat(req: ChatRequest):
        result = await chat(req.message, req.session_id)
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        return {"response": "\n\n".join(parts), "messages": result}

    # --- OpenAI-compatible chat completions --------------------------------
    # Lets this agent be registered as a model in the LiteLLM gateway /
    # OpenWebUI without any protocol adapter.
    @fastapi_app.post("/v1/chat/completions")
    async def _openai_chat_completions(req: dict):
        messages = req.get("messages", [])
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return {"error": "No user message provided"}, 400
        prompt = user_msgs[-1].get("content", "")
        session_id = f"openai-compat-{int(time.time())}"
        stream = req.get("stream", False)

        result = await chat(prompt, session_id)
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        content = "\n\n".join(parts)
        created = int(time.time())
        completion_id = f"{AGENT_NAME}-{session_id}"

        if stream:
            async def _stream():
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": AGENT_NAME,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                done_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": AGENT_NAME,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(done_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_stream(), media_type="text/event-stream")

        return {
            "id": completion_id, "object": "chat.completion",
            "created": created, "model": AGENT_NAME,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @fastapi_app.get("/v1/models")
    async def _openai_models():
        return {
            "object": "list",
            "data": [{"id": AGENT_NAME, "object": "model", "created": 1774600000, "owned_by": "protolabs"}],
        }

    # --- A2A agent card -----------------------------------------------------
    @fastapi_app.get("/.well-known/agent.json", include_in_schema=False)
    @fastapi_app.get("/.well-known/agent-card.json", include_in_schema=False)
    async def _a2a_agent_card(request: Request):
        host = request.headers.get("host", f"{AGENT_NAME}:7870")
        return JSONResponse(
            content=_build_agent_card(host),
            headers={"Cache-Control": "public, max-age=60"},
        )

    # --- A2A protocol -------------------------------------------------------
    # JSON-RPC + REST, streaming, polling, cancel, push webhooks.
    from a2a_handler import register_a2a_routes

    auth_env = f"{AGENT_NAME.upper()}_API_KEY"
    register_a2a_routes(
        app=fastapi_app,
        chat_stream_fn_factory=_chat_langgraph_stream,
        chat_fn=chat,
        api_key=os.environ.get(auth_env, ""),
        agent_card={},
        register_card_route=False,  # card is already served above
    )

    # --- Prometheus metrics -------------------------------------------------
    if metrics.is_enabled():
        try:
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
            from fastapi import Response as FastAPIResponse

            @fastapi_app.get("/metrics", include_in_schema=False)
            async def _prometheus_metrics():
                return FastAPIResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
        except ImportError:
            pass

    # --- Static + PWA assets -----------------------------------------------
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        manifest_path = static_dir / "manifest.json"
        if manifest_path.exists():
            @fastapi_app.get("/manifest.json", include_in_schema=False)
            async def _serve_manifest() -> FileResponse:
                return FileResponse(str(manifest_path), media_type="application/manifest+json")

        sw_path = static_dir / "sw.js"
        if sw_path.exists():
            @fastapi_app.get("/sw.js", include_in_schema=False)
            async def _serve_sw() -> FileResponse:
                return FileResponse(
                    str(sw_path), media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/"},
                )

        fastapi_app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- Mount Gradio at root -----------------------------------------------
    app = gr.mount_gradio_app(
        fastapi_app, blocks, path="/",
        footer_links=[],
        favicon_path=str(static_dir / "favicon.svg") if (static_dir / "favicon.svg").exists() else None,
    )

    log.info("Starting %s on http://0.0.0.0:%d", AGENT_NAME, args.port)
    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    _main()
