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

import re

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

Optionally, after `</output>`, you may self-report confidence:

    <confidence>0.85</confidence>
    <confidence_explanation>one short sentence on what drove the score</confidence_explanation>

- `<confidence>` is a number in [0, 1] — your honest self-assessment of
  whether the answer is correct/complete. Omit it if you'd only be guessing.
- `<confidence_explanation>` is optional. Neither tag is shown to the user;
  they ride a confidence-v1 DataPart on the A2A artifact.
""".strip()


_OUTPUT_RE = re.compile(r"<output>([\s\S]*?)</output>", re.IGNORECASE)
_SCRATCH_RE = re.compile(r"<scratch_pad>[\s\S]*?</scratch_pad>", re.IGNORECASE)
_ORPHAN_SCRATCH_OPEN_RE = re.compile(r"<scratch_pad>[\s\S]*$", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_ORPHAN_THINK_OPEN_RE = re.compile(r"<think>[\s\S]*$", re.IGNORECASE)
_ORPHAN_THINK_CLOSE_RE = re.compile(r"</think>\s*", re.IGNORECASE)
_CONFIDENCE_BLOCK_RE = re.compile(r"<confidence>[\s\S]*?</confidence>", re.IGNORECASE)
_CONFIDENCE_EXPL_BLOCK_RE = re.compile(
    r"<confidence_explanation>[\s\S]*?</confidence_explanation>", re.IGNORECASE,
)
_CONFIDENCE_RE = re.compile(r"<confidence>\s*(-?[\d.]+)\s*</confidence>", re.IGNORECASE)
_CONFIDENCE_EXPLANATION_RE = re.compile(
    r"<confidence_explanation>([\s\S]*?)</confidence_explanation>", re.IGNORECASE,
)


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
    # Confidence tags ride a DataPart, never the user-facing text. Strip them
    # in case the model emits them inside (or right after) <output>.
    text = _CONFIDENCE_EXPL_BLOCK_RE.sub("", text)
    text = _CONFIDENCE_BLOCK_RE.sub("", text)
    return text


def extract_confidence(text: str) -> tuple[float | None, str | None]:
    """Parse an optional self-reported ``<confidence>`` (and explanation).

    Returns ``(confidence, explanation)`` where confidence is a float or
    None (malformed/absent) and explanation is a stripped string or None.
    The A2A handler clamps confidence to [0, 1] on write.
    """
    confidence: float | None = None
    m = _CONFIDENCE_RE.search(text)
    if m:
        try:
            confidence = float(m.group(1))
        except ValueError:
            confidence = None
    explanation: str | None = None
    me = _CONFIDENCE_EXPLANATION_RE.search(text)
    if me:
        explanation = me.group(1).strip() or None
    return confidence, explanation


def extract_output(text: str) -> str:
    """Return the user-facing content from a complete model response.

    Order of preference:
    1. Content inside the first ``<output>...</output>`` pair (still
       with any nested reasoning markers stripped).
    2. Full text with all reasoning markers stripped — covers the case
       where the model skipped ``<output>`` but still wrapped scratch.
    3. Raw text if none of the above triggers — the model ignored every
       convention. Rare in practice once the prompt is in place.
    """
    m = _OUTPUT_RE.search(text)
    if m:
        return _strip_reasoning(m.group(1)).strip()
    return _strip_reasoning(text).strip()
