"""cost-v1 DataPart: cache fields + costUsd emission (ADR 0006 Slice 1).

Verifies the terminal artifact carries Workstacean's Anthropic-shaped cache
fields and a top-level costUsd, and that metrics.record_llm_call accepts the
enriched signature without a live Prometheus registry.
"""

from __future__ import annotations

import pytest

from a2a_handler import COST_EXT_URI, COST_MIME, _cost_payload


def _record_with_usage(**usage):
    """Minimal TaskRecord-like stub for _cost_payload (reads .usage,
    .created_at, .updated_at)."""
    from a2a_handler import TaskRecord

    rec = TaskRecord(
        id="t1", context_id="c1", state="completed",
        created_at="2026-06-01T00:00:00+00:00",
        updated_at="2026-06-01T00:00:02+00:00",
        message_text="hi",
    )
    rec.usage = usage
    return rec


def test_cost_payload_includes_cache_fields_and_costusd() -> None:
    payload = _cost_payload(_record_with_usage(
        input_tokens=1500, output_tokens=420, total_tokens=1920,
        cache_read_input_tokens=900, cache_creation_input_tokens=100,
        cost_usd=0.0123,
    ))
    assert payload is not None
    # usage block carries the Anthropic-shaped cache fields...
    assert payload["usage"]["cache_read_input_tokens"] == 900
    assert payload["usage"]["cache_creation_input_tokens"] == 100
    # ...but NOT the internal cost accumulator (that's lifted to top-level).
    assert "cost_usd" not in payload["usage"]
    assert payload["costUsd"] == 0.0123
    assert isinstance(payload["durationMs"], int)


def test_cost_payload_omits_costusd_when_zero() -> None:
    payload = _cost_payload(_record_with_usage(
        input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.0,
    ))
    assert payload is not None
    assert "costUsd" not in payload


def test_cost_payload_none_when_no_tokens() -> None:
    assert _cost_payload(_record_with_usage(input_tokens=0, output_tokens=0, total_tokens=0)) is None


def test_extension_uri_is_the_canonical_workstacean_uri() -> None:
    # Must match protoWorkstacean's COST_URI for its interceptor to engage.
    assert COST_EXT_URI == "https://proto-labs.ai/a2a/ext/cost-v1"
    assert COST_MIME == "application/vnd.protolabs.cost-v1+json"


def test_record_llm_call_accepts_enriched_signature_when_disabled() -> None:
    import metrics

    # No init() in tests → disabled → no-op, but the signature must accept the
    # new cache/cost kwargs without error.
    metrics.record_llm_call(
        "claude-opus-4-8", "stop", 1.2,
        tokens_input=100, tokens_output=50,
        cache_read=60, cache_creation=10, cost_usd=0.002,
    )
