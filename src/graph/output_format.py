"""Leaked-reasoning guard for model output.

The model answers **naturally** — its reasoning streams on the gateway's native
``reasoning_content`` channel (lifted by ``graph.llm._ReasoningChatOpenAI`` and
forwarded as ``reasoning`` frames by ``server.chat``), so there is no
``<scratch_pad>``/``<output>`` text protocol anymore. The lead agent dropped it in
#1328; the subagents in the de-protocol pass. ``OUTPUT_FORMAT_INSTRUCTIONS`` is gone
and the model is never told to emit those tags.

The one residual job is a thin guard: some gateway/model combos still leak raw
``<think>...</think>`` (or ``<scratch_pad>...``) blocks into the answer **content**
channel (notably MiniMax via LiteLLM bug #22392). ``strip_reasoning`` removes those
balanced blocks so leaked reasoning never reaches A2A artifacts, the console,
subagent return values, or — the ADR-0021 guardrail — persisted knowledge / session
memory. Backtick-guarded so an answer that *mentions* a tag in inline code
(``answer in `<output>` format``) is never mangled.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("protoagent.output_format")

# Balanced reasoning blocks a provider may leak into the answer content channel
# (LiteLLM #22392 — MiniMax emits raw ``<think>...</think>`` as content). The leak is
# never backtick-wrapped, so ``(?<!`)`` keeps a literal tag *mention* in the answer.
_THINK_RE = re.compile(r"(?<!`)<think>[\s\S]*?</think>", re.IGNORECASE)
_SCRATCH_RE = re.compile(r"(?<!`)<scratch_pad>[\s\S]*?</scratch_pad>", re.IGNORECASE)
# Truncated/orphan leaks — a reasoning leak cut off mid-block (no close), or a stray
# close. Eat to end; backtick-guarded so a literal tag *mention* in the answer is not
# treated as a leak and truncated.
_ORPHAN_THINK_OPEN_RE = re.compile(r"(?<!`)<think>[\s\S]*$", re.IGNORECASE)
_ORPHAN_THINK_CLOSE_RE = re.compile(r"</think>\s*", re.IGNORECASE)
_ORPHAN_SCRATCH_OPEN_RE = re.compile(r"(?<!`)<scratch_pad>[\s\S]*$", re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Remove leaked ``<think>`` / ``<scratch_pad>`` reasoning (balanced or truncated/
    orphan) from a model response.

    Idempotent — real user content never contains these literal tags, so applying it
    twice is safe.
    """
    text = _THINK_RE.sub("", text)
    text = _ORPHAN_THINK_OPEN_RE.sub("", text)
    text = _ORPHAN_THINK_CLOSE_RE.sub("", text)
    text = _SCRATCH_RE.sub("", text)
    text = _ORPHAN_SCRATCH_OPEN_RE.sub("", text)
    return text


def strip_reasoning(text: str) -> str:
    """Public leaked-reasoning stripper for *storage* guardrails (ADR 0021): leaked
    provider reasoning must never persist to the knowledge base / session memory. Keeps
    everything else intact. Idempotent."""
    return _strip_reasoning(text or "")


def extract_output(text: str) -> str:
    """The user-facing answer from a complete model response.

    The model answers natively (no ``<output>`` protocol), so this just strips any
    leaked provider reasoning and returns the clean, trimmed text.
    """
    return _strip_reasoning(text or "").strip()
