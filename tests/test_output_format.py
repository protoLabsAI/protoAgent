"""graph.output_format — the thin leaked-reasoning guard.

The ``<scratch_pad>``/``<output>`` protocol is retired; the model answers natively
(reasoning on the gateway's ``reasoning_content`` channel). The only job left is
stripping LEAKED balanced ``<think>`` / ``<scratch_pad>`` blocks (LiteLLM #22392 —
MiniMax leaks raw ``<think>`` into the answer content channel) so reasoning never
reaches A2A artifacts, the console, subagent returns, or persisted storage (ADR 0021).
"""

from __future__ import annotations

from graph.output_format import extract_output, strip_reasoning


def test_extract_output_passthrough_clean_answer():
    assert extract_output("just a plain native answer, no tags") == "just a plain native answer, no tags"


def test_extract_output_strips_leaked_balanced_think():
    # LiteLLM #22392: MiniMax leaks raw <think>...</think> into the content channel.
    assert extract_output("<think>internal reasoning</think>The answer.") == "The answer."
    assert extract_output("head <think>leak</think> tail") == "head  tail"


def test_extract_output_strips_leaked_balanced_scratch_pad():
    assert extract_output("<scratch_pad>planning</scratch_pad>The answer.") == "The answer."


def test_extract_output_keeps_backticked_tag_mention():
    # A self-describing answer that *mentions* the tags in inline code must survive —
    # the strip is backtick-guarded so it isn't treated as a leak and truncated.
    text = "I reason in `<think>` and `<scratch_pad>` blocks, then write the answer."
    assert extract_output(text) == text


def test_extract_output_empty_input():
    assert extract_output("") == ""
    assert extract_output("   \n ") == ""


def test_strip_reasoning_is_storage_guard_and_idempotent():
    text = "<think>THINK</think>real<scratch_pad>SCRATCH</scratch_pad>content"
    once = strip_reasoning(text)
    assert once == "realcontent"
    assert strip_reasoning(once) == once  # idempotent
    assert "THINK" not in once and "SCRATCH" not in once
    assert strip_reasoning(None) == ""  # None-safe


def test_strip_reasoning_keeps_non_reasoning_content():
    assert strip_reasoning("a normal note about the project") == "a normal note about the project"
