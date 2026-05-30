"""Tests for graph.output_format — <scratch_pad>/<output> protocol.

Covers the three shapes of traffic we see live:

1. Well-behaved model — emits both tags in the documented order.
2. Mixed — emits `<scratch_pad>` but forgets `<output>` wrapper.
3. Native thinking — provider (MiniMax, DeepSeek, Qwen3) leaks
   `<think>...</think>` regions that the filter must also strip.

The one-shot terminal path runs the complete text through ``extract_output``.
The incremental path (``stream_visible_output``) streams the user-facing
``<output>`` region token-by-token without leaking ``<scratch_pad>``; the
terminal ``extract_output`` reconciles any held-back tail. Both are covered.
"""

from __future__ import annotations

from graph.output_format import (
    OUTPUT_FORMAT_INSTRUCTIONS,
    _strip_reasoning,
    extract_output,
    stream_visible_output,
)


def test_extract_output_happy_path():
    text = "<scratch_pad>reasoning here</scratch_pad>\n<output>the answer</output>"
    assert extract_output(text) == "the answer"


def test_extract_output_strips_scratch_when_output_missing():
    text = "<scratch_pad>reasoning</scratch_pad>\nthe answer without output tag"
    assert extract_output(text) == "the answer without output tag"


def test_extract_output_strips_orphan_scratch_open():
    """MiniMax M2.x sometimes leaves scratch_pad unclosed — treat it as
    'everything from the orphan to EOT is reasoning' and strip it."""
    text = "real prose here <scratch_pad>unfinished reasoning never closed"
    assert extract_output(text) == "real prose here"


def test_extract_output_passthrough_no_tags():
    text = "just a plain response with no tags"
    assert extract_output(text) == "just a plain response with no tags"


def test_extract_output_takes_first_output_block():
    text = "<output>first</output> junk <output>second</output>"
    assert extract_output(text) == "first"


def test_extract_output_is_case_insensitive():
    assert extract_output("<OUTPUT>x</OUTPUT>") == "x"


def test_extract_output_strips_think_inside_output():
    """LiteLLM #22392: MiniMax leaks `<think>...</think>` blocks inside
    `<output>`. _strip_reasoning runs over the output region too."""
    text = (
        "<output>head <think>inner reasoning</think> tail</output>"
    )
    assert extract_output(text) == "head  tail"


def test_extract_output_strips_orphan_think():
    """Orphaned `<think>` opening with no close — drop to EOT."""
    text = "<output>visible <think>unfinished reasoning"
    # Output is unclosed, falls to passthrough branch which strips orphan think
    result = extract_output(text)
    assert "<think>" not in result
    assert "unfinished" not in result
    assert "visible" in result


def test_extract_output_strips_orphan_think_close():
    """Orphaned `</think>` (opener was somewhere upstream already)."""
    text = "<output>real answer</think></output>"
    assert extract_output(text) == "real answer"


def test_strip_reasoning_idempotent():
    """Real content never contains literal tag markers, so applying
    _strip_reasoning twice is safe and produces the same result."""
    text = "<think>THINK_BODY</think>real<scratch_pad>SCRATCH_BODY</scratch_pad>content"
    once = _strip_reasoning(text)
    twice = _strip_reasoning(once)
    assert once == twice
    assert "THINK_BODY" not in once
    assert "SCRATCH_BODY" not in once
    assert once == "realcontent"


def test_instructions_mention_both_tags():
    """Sanity check — the prompt fragment must teach both tags."""
    assert "<scratch_pad>" in OUTPUT_FORMAT_INSTRUCTIONS
    assert "<output>" in OUTPUT_FORMAT_INSTRUCTIONS


def test_extract_output_recovers_truncated_orphan_output():
    """max_tokens hit mid-<output>: no closing tag. Tier 2 recovers the
    partial answer from the opener to EOT, scratch stripped."""
    text = "<scratch_pad>planning the reply</scratch_pad><output>The partial answer that got cut o"
    assert extract_output(text) == "The partial answer that got cut o"


def test_extract_output_empty_when_scratch_only(caplog):
    """Scratch-only with no output → empty (never leak reasoning), and a
    WARNING diagnostic so the operator can see the turn went silent."""
    import logging

    with caplog.at_level(logging.WARNING, logger="protoagent.output_format"):
        assert extract_output("<scratch_pad>only reasoning, never committed</scratch_pad>") == ""
    assert any("empty after stripping" in r.message for r in caplog.records)


def test_extract_output_empty_input_returns_empty():
    assert extract_output("") == ""
    assert extract_output("   \n  ") == ""


# ── dropped-turn detection ────────────────────────────────────────────────────

from graph.output_format import DROPPED_SCRATCH_KICKER, is_dropped_scratch_turn


def test_dropped_scratch_only_is_detected():
    assert is_dropped_scratch_turn("<scratch_pad>thinking, never committed</scratch_pad>") is True


def test_dropped_think_only_is_detected():
    """Qwen-style <think> with no output is also a drop."""
    assert is_dropped_scratch_turn("<think>reasoning</think>") is True


def test_not_dropped_when_output_present():
    assert is_dropped_scratch_turn("<scratch_pad>x</scratch_pad><output>answer</output>") is False


def test_not_dropped_when_no_reasoning_markers():
    assert is_dropped_scratch_turn("plain text answer") is False
    assert is_dropped_scratch_turn("") is False


def test_kicker_is_actionable():
    k = DROPPED_SCRATCH_KICKER.lower()
    assert "<output>" in k and ("tool" in k)


# ── stream_visible_output (incremental token streaming) ──────────────────────


def test_stream_visible_empty_while_in_scratch():
    """Before <output> opens, nothing user-facing is streamed — the scratch_pad
    must never leak token-by-token."""
    assert stream_visible_output("<scratch_pad>still thinking about") == ""
    assert stream_visible_output("") == ""
    assert stream_visible_output("no tags yet") == ""


def test_stream_visible_orphan_open_streams_content():
    """An open <output> with no close yet (mid-stream) surfaces its content."""
    assert stream_visible_output("<scratch_pad>x</scratch_pad><output>Hello there") == "Hello there"


def test_stream_visible_closed_output():
    assert stream_visible_output("<output>the answer</output>") == "the answer"


def test_stream_visible_holds_back_partial_closing_tag():
    """A half-written </output> must not flash on screen."""
    assert stream_visible_output("<output>done </out") == "done "
    assert stream_visible_output("<output>done<") == "done"


def test_stream_visible_holds_back_partial_confidence_tag():
    assert stream_visible_output("<output>answer</output><conf") == "answer"


def test_stream_visible_strips_think_regions():
    assert stream_visible_output("<output>A<think>noise</think>B</output>") == "AB"
    assert stream_visible_output("<output>A<think>partial reasoning") == "A"


def test_stream_visible_keeps_legit_angle_brackets():
    """A '<' followed later by '>' is real content, not a partial tag."""
    assert stream_visible_output("<output>a < b > c") == "a < b > c"


def test_stream_visible_is_monotonic_prefix():
    """As raw grows, the visible result only extends — so a caller can safely
    emit result[already_emitted:] each step."""
    chunks = ["<output>", "Hel", "lo wor", "ld</output><confidence>0.9</confidence>"]
    raw = ""
    seen = ""
    for c in chunks:
        raw += c
        vis = stream_visible_output(raw)
        assert vis.startswith(seen) or seen.startswith(vis)  # never diverges
        seen = vis
    assert seen == "Hello world"
    # The terminal extractor agrees with the final streamed text.
    assert extract_output(raw) == "Hello world"
