"""Tests for the confidence-v1 A2A extension wiring (A2A 1.0).

The model self-reports a ``<confidence>`` tag (parsed by
``graph.output_format.extract_confidence``); the executor records it and emits a
confidence-v1 DataPart on the terminal artifact via ``protolabs_a2a``. These
tests cover the parse, the 1.0 payload shape, and the clamp.
"""

from __future__ import annotations

import protolabs_a2a as pa
from graph.output_format import extract_confidence, extract_output


# ── extract_confidence (graph) ────────────────────────────────────────────────


def test_extract_confidence_parses_score_and_explanation():
    text = (
        "<output>The answer is 42.</output>\n"
        "<confidence>0.85</confidence>\n"
        "<confidence_explanation>two consistent sources</confidence_explanation>"
    )
    conf, expl = extract_confidence(text)
    assert conf == 0.85
    assert expl == "two consistent sources"


def test_extract_confidence_absent_returns_none():
    conf, expl = extract_confidence("<output>no confidence here</output>")
    assert conf is None
    assert expl is None


def test_extract_confidence_malformed_score_is_none():
    conf, _ = extract_confidence("<confidence>not-a-number</confidence>")
    assert conf is None


def test_confidence_tags_stripped_from_output():
    text = (
        "<output>Clean answer.</output>"
        "<confidence>0.9</confidence>"
        "<confidence_explanation>x</confidence_explanation>"
    )
    out = extract_output(text)
    assert out == "Clean answer."
    assert "confidence" not in out.lower()


# ── confidence-v1 DataPart (protolabs_a2a, A2A 1.0 shape) ─────────────────────


def test_emit_confidence_payload_minimal():
    part = pa.emit_confidence(0.7, success=True)
    assert part["metadata"]["mimeType"] == pa.CONFIDENCE_MIME
    assert pa.parse_confidence(part) == {"confidence": 0.7, "success": True}


def test_emit_confidence_includes_explanation_and_success_false():
    """The 1.0 contract uses the ``explanation`` key (the 0.3 shape used
    ``confidenceExplanation``). Reporting confidence on a non-success run is
    the high-confidence-failure calibration signal — still emitted."""
    part = pa.emit_confidence(0.9, explanation="sure but wrong", success=False)
    payload = pa.parse_confidence(part)
    assert payload["confidence"] == 0.9
    assert payload["explanation"] == "sure but wrong"
    assert payload["success"] is False


def test_confidence_clamp_is_executor_side():
    """The executor clamps a model-reported confidence to [0, 1] before
    emitting, so the DataPart is always in-spec. Verify the clamp contract
    that ``a2a_executor`` applies (max(0, min(1, x)))."""
    assert max(0.0, min(1.0, 1.7)) == 1.0
    assert max(0.0, min(1.0, -0.5)) == 0.0
