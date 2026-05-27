"""Structured output protocol for protoAgent — `<scratch_pad>` / `<output>` tags.

The model is instructed to wrap internal deliberation in ``<scratch_pad>``
and the user-facing answer in ``<output>``. Server-side, we parse those
tags and forward only the ``<output>`` content to consumers (A2A
artifacts, Gradio chat, subagent return values).

We deliberately do NOT parse the protocol mid-stream — chunk-boundary
tag splitting turned that into a state-machine rabbit hole and the
per-token text rendering consumers were doing didn't add real value.
Instead, ``_chat_langgraph_stream`` accumulates the model's tokens
silently while still emitting tool-start / tool-end status events, then
passes the complete text through ``extract_output`` once on the
terminal ``done`` frame. The consumer sees tool progress during the run
and the clean final artifact at completion.

``_strip_reasoning`` also removes provider-emitted ``<think>...</think>``
regions (LiteLLM bug #22392 leaks these as raw tags from MiniMax) and
any orphaned scratch_pad / think openings.

The prompt fragment that teaches the protocol to the model lives in
``OUTPUT_FORMAT_INSTRUCTIONS`` below; ``graph.prompts`` appends it to
both the lead agent and subagent system prompts.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("protoagent.output_format")

OUTPUT_FORMAT_INSTRUCTIONS = """
# Response format

Structure every response as:

    <scratch_pad>
    Internal reasoning — which tools to call, what you're learning from
    each result, how you'll assemble the final answer. This is not shown
    to the user; use it freely to think.
    </scratch_pad>
    <output>
    The user-facing answer. This is what lands in the A2A artifact /
    Discord / Gradio chat. Be clean, scannable, markdown-formatted.
    </output>

Rules:
- Always emit both tags, in that order, exactly once.
- Never include literal `<scratch_pad>` or `<output>` markers inside the
  user-facing content.
- Keep tool-calling deliberation in `<scratch_pad>`. Keep only the
  finished, customer-ready answer in `<output>`.
- If you must defer or ask for clarification, put the question inside
  `<output>` too — the user never sees `<scratch_pad>`.
""".strip()


_OUTPUT_RE = re.compile(r"<output>([\s\S]*?)</output>", re.IGNORECASE)
_SCRATCH_RE = re.compile(r"<scratch_pad>[\s\S]*?</scratch_pad>", re.IGNORECASE)
_ORPHAN_SCRATCH_OPEN_RE = re.compile(r"<scratch_pad>[\s\S]*$", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_ORPHAN_THINK_OPEN_RE = re.compile(r"<think>[\s\S]*$", re.IGNORECASE)
_ORPHAN_THINK_CLOSE_RE = re.compile(r"</think>\s*", re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Remove all reasoning markers (``<think>``, ``<scratch_pad>``, and
    orphaned variants) from a complete response.

    Idempotent — real user content should never contain literal tag
    markers, so applying this twice is safe.
    """
    text = _THINK_RE.sub("", text)
    text = _ORPHAN_THINK_OPEN_RE.sub("", text)
    text = _ORPHAN_THINK_CLOSE_RE.sub("", text)
    text = _SCRATCH_RE.sub("", text)
    text = _ORPHAN_SCRATCH_OPEN_RE.sub("", text)
    return text


_ORPHAN_OUTPUT_OPEN_RE = re.compile(r"<output>([\s\S]*)$", re.IGNORECASE)


def extract_output(text: str) -> str:
    """Return the user-facing content from a complete model response.

    Order of preference:
    1. Content inside the first ``<output>...</output>`` pair (with any
       nested reasoning markers stripped).
    2. Orphan-open ``<output>`` with no closing tag — recovers responses
       truncated mid-output when ``max_tokens`` is hit. Everything from the
       opener to end of text, scratch stripped.
    3. Full text with all reasoning markers stripped — covers the case
       where the model skipped ``<output>`` but still wrapped scratch.

    Returns "" when every strategy yields empty, logging a WARNING with a
    sanitized preview so operators can tell *why* a turn went silent
    (truncated mid-scratch vs. truly empty vs. odd shape). ``scratch_pad`` is
    never surfaced — leaking internal reasoning breaks the protocol contract.
    """
    if not text or not text.strip():
        return ""

    # 1. Closed <output>...</output>
    m = _OUTPUT_RE.search(text)
    if m:
        cleaned = _strip_reasoning(m.group(1)).strip()
        if cleaned:
            return cleaned

    # 2. Orphan <output> opener (max_tokens truncation mid-output).
    orphan = _ORPHAN_OUTPUT_OPEN_RE.search(text)
    if orphan:
        cleaned = _strip_reasoning(orphan.group(1)).strip()
        if cleaned:
            return cleaned

    # 3. Last resort — strip reasoning, return what's left.
    fallback = _strip_reasoning(text).strip()
    if fallback:
        return fallback

    preview = text[:400].replace("\n", "\\n")
    log.warning(
        "[extract_output] empty after stripping — len=%d scratch=%s "
        "output=%s preview=%r",
        len(text),
        "<scratch_pad>" in text.lower(),
        "<output>" in text.lower(),
        preview,
    )
    return ""
