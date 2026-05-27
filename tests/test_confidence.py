"""Tests for the confidence-v1 A2A extension wiring."""

import pytest

from a2a_handler import (
    CANCELED,
    COMPLETED,
    CONFIDENCE_MIME,
    A2ATaskStore,
    TaskRecord,
    _confidence_payload,
    _terminal_artifact_parts,
)
from graph.output_format import extract_confidence, extract_output


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _record(**kw) -> TaskRecord:
    d = dict(
        id="t", context_id="c", state=COMPLETED,
        created_at=_now(), updated_at=_now(), message_text="hi",
    )
    d.update(kw)
    return TaskRecord(**d)


# ── extract_confidence ────────────────────────────────────────────────────────

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


# ── _confidence_payload ───────────────────────────────────────────────────────

def test_payload_none_when_no_confidence():
    assert _confidence_payload(_record(confidence=None)) is None


def test_payload_success_true_on_completed():
    p = _confidence_payload(_record(state=COMPLETED, confidence=0.7))
    assert p == {"confidence": 0.7, "success": True}


def test_payload_success_false_on_non_completed_includes_explanation():
    p = _confidence_payload(
        _record(state=CANCELED, confidence=0.9, confidence_explanation="sure but wrong")
    )
    assert p["success"] is False
    assert p["confidence"] == 0.9
    assert p["confidenceExplanation"] == "sure but wrong"


def test_terminal_artifact_includes_confidence_datapart():
    parts = _terminal_artifact_parts(
        _record(accumulated_text="done", confidence=0.6)
    )
    data_parts = [p for p in parts if p.get("metadata", {}).get("mimeType") == CONFIDENCE_MIME]
    assert len(data_parts) == 1
    assert data_parts[0]["data"]["confidence"] == 0.6


def test_terminal_artifact_omits_confidence_when_unset():
    parts = _terminal_artifact_parts(_record(accumulated_text="done"))
    assert not any(
        p.get("metadata", {}).get("mimeType") == CONFIDENCE_MIME for p in parts
    )


# ── set_confidence clamping ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_confidence_clamps_and_stores():
    store = A2ATaskStore()
    rec = await store.create(_record(id="conf-task", state="working"))
    await store.set_confidence(rec.id, confidence=1.7, explanation="  over  ")
    got = await store.get(rec.id)
    assert got.confidence == 1.0  # clamped
    assert got.confidence_explanation == "over"

    await store.set_confidence(rec.id, confidence=-0.5)
    got = await store.get(rec.id)
    assert got.confidence == 0.0
