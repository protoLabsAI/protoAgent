"""KnowledgeIngestMiddleware — auto-capture tool output into the knowledge store.

After a tool runs, optionally extract findings from its output and persist them
to the KB (``domain='finding'``) so later turns can recall them. Generalised
from the protoLabs fleet (pwnDeck), stripped of its security-domain extractor:

- **Pluggable extractor** — ``extractor(tool_name, output) -> list[str]`` lets a
  fork distil structured findings (e.g. a small/fast LLM). With no extractor,
  the deterministic fallback stores the (truncated) raw output as one finding.
- **Fire-and-forget** — ingestion never raises into the agent loop; a capture
  failure is logged and skipped.
- **Opt-in** — off by default; wired only when ``middleware.ingest: true``.

Skips empty output and tool errors / policy blocks so junk doesn't pollute the
store.
"""

from __future__ import annotations

import logging
from typing import Callable

from langchain.agents.middleware import AgentMiddleware

log = logging.getLogger(__name__)

# extractor(tool_name, output) -> list of finding strings (empty = nothing to keep)
IngestExtractor = Callable[[str, str], "list[str]"]

_SKIP_PREFIXES = ("Error", "Blocked by policy", "[BLOCKED]")


class KnowledgeIngestMiddleware(AgentMiddleware):
    """Capture tool output into the knowledge store after execution."""

    def __init__(
        self,
        knowledge_store,
        *,
        extractor: IngestExtractor | None = None,
        ingest_tools: set[str] | list[str] | None = None,
        max_chars: int = 4000,
    ):
        super().__init__()
        self._store = knowledge_store
        self._extractor = extractor
        self._ingest_tools = set(ingest_tools) if ingest_tools else None  # None = all
        self._max_chars = max_chars

    def wrap_tool_call(self, request, handler):
        result = handler(request)
        self._try_ingest(request, result)
        return result

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        self._try_ingest(request, result)
        return result

    def _try_ingest(self, request, result) -> None:
        try:
            if self._store is None:
                return
            tool_name = request.tool_call.get("name", "")
            if self._ingest_tools is not None and tool_name not in self._ingest_tools:
                return
            content = getattr(result, "content", None)
            if content is None:
                return
            content = str(content)
            if not content.strip() or content.startswith(_SKIP_PREFIXES):
                return
            if len(content) > self._max_chars:
                content = content[: self._max_chars] + "\n… [truncated]"

            findings: list[str] = []
            if self._extractor is not None:
                try:
                    findings = self._extractor(tool_name, content) or []
                except Exception as exc:  # noqa: BLE001 - extractor is fork code
                    log.debug("[ingest] extractor failed for %s: %s", tool_name, exc)
            if not findings:
                findings = [content]  # deterministic fallback: keep the raw output

            for f in findings:
                self._store.add_finding(
                    content=str(f),
                    source=f"tool:{tool_name}",
                    source_type="tool_output",
                    finding_type="ingest",
                )
        except Exception as exc:  # noqa: BLE001 - never break the agent loop
            log.debug("[ingest] skipped: %s", exc)
