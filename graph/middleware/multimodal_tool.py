"""MultimodalToolResultMiddleware — rewrite opt-in multimodal tool results (#1930).

The tool loop's ToolMessages are text-only, so a vision model could never SEE an
image a tool just produced. A tool opts in by returning the sentinel envelope
built by ``graph.multimodal.multimodal_tool_result``; this middleware detects
the sentinel on the finished ``ToolMessage`` and rewrites its content —
``image_url`` content blocks when the active model is vision-capable
(``model.vision: true``), the text part alone (optionally via the
``knowledge.image_describe_model`` describe path, #1381) when it isn't.

Always installed, and provably inert for every ordinary tool: the hot path per
tool call is one ``isinstance`` + one ``startswith`` on a control character no
model or tool emits by accident — a non-envelope result is returned as the SAME
object, untouched. Guardrails (image count / byte caps) live in
``graph.multimodal`` and are enforced during the rewrite.
"""

from __future__ import annotations

import asyncio
import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from graph.multimodal import DescribeFn, is_multimodal_result, parse_multimodal_result, render_multimodal_content

logger = logging.getLogger(__name__)


class MultimodalToolResultMiddleware(AgentMiddleware):
    """Rewrite sentinel-enveloped tool results into vision content blocks (or degrade to text)."""

    def __init__(self, *, vision: bool = False, describe_fn: DescribeFn | None = None):
        super().__init__()
        self._vision = vision
        self._describe_fn = describe_fn

    def _rewrite(self, message: ToolMessage) -> ToolMessage:
        """Envelope → final content. Never raises: any failure degrades to a text
        note (the raw multi-MB envelope must not stay in context either way)."""
        try:
            env = parse_multimodal_result(message.content)
            if env is None:  # unreachable via the guarded callers; belt-and-braces
                return message
            content = render_multimodal_content(env, vision=self._vision, describe_fn=self._describe_fn)
        except Exception:  # noqa: BLE001 — a rewrite bug must not kill the tool loop
            logger.exception("[multimodal] tool-result rewrite failed; degrading to a text note")
            content = "[multimodal tool result could not be processed; its images were dropped]"
        return message.model_copy(update={"content": content})

    def wrap_tool_call(self, request, handler):
        result = handler(request)
        if isinstance(result, ToolMessage) and is_multimodal_result(result.content):
            return self._rewrite(result)
        return result

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        if isinstance(result, ToolMessage) and is_multimodal_result(result.content):
            # Off-loop: the rewrite may base64-decode megabytes, read a file, or
            # call the (sync, blocking) describe model.
            return await asyncio.to_thread(self._rewrite, result)
        return result


def build_multimodal_middleware(config, *, vision: bool | None = None) -> MultimodalToolResultMiddleware:
    """The wired-from-config build both agent chains use (lead + subagent).

    ``vision`` defaults to ``config.model_vision`` (the existing native-vision
    flag the inbound path gates on); pass an override for a chain whose model
    differs from the main one (a subagent on a text-only aux model must NOT get
    image blocks). The describe fallback is built only when the effective model
    is text-only — that's the only path that uses it."""
    if vision is None:
        vision = bool(getattr(config, "model_vision", False))
    describe = None
    if not vision:
        try:
            from graph.llm import create_describe_image_fn

            describe = create_describe_image_fn(config)  # None unless knowledge.image_describe_model is set
        except Exception:  # noqa: BLE001 — a describe-model misconfig must not break graph build
            logger.warning("[multimodal] describe-image fallback unavailable", exc_info=True)
    return MultimodalToolResultMiddleware(vision=vision, describe_fn=describe)
