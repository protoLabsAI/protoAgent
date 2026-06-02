"""cost-v1 DataPart: cache fields + costUsd emission (ADR 0006 Slice 1, A2A 1.0).

The terminal artifact carries Workstacean's Anthropic-shaped cache fields and a
top-level costUsd. The bespoke ``_cost_payload`` was replaced by
``protolabs_a2a.emit_cost``; the executor accumulates usage across LLM calls and
passes it in. Also verifies ``metrics.record_llm_call`` accepts the enriched
signature without a live Prometheus registry.
"""

from __future__ import annotations

import protolabs_a2a as pa


def test_cost_payload_includes_cache_fields_and_costusd() -> None:
    part = pa.emit_cost(
        {
            "input_tokens": 1500, "output_tokens": 420,
            "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100,
        },
        duration_ms=2000,
        cost_usd=0.0123,
        success=True,
    )
    payload = pa.parse_cost(part)
    assert payload is not None
    # usage block carries the Anthropic-shaped cache fields...
    assert payload["usage"]["cache_read_input_tokens"] == 900
    assert payload["usage"]["cache_creation_input_tokens"] == 100
    # ...and the dollar cost is top-level (not inside usage).
    assert "cost_usd" not in payload["usage"]
    assert payload["costUsd"] == 0.0123
    assert payload["durationMs"] == 2000


def test_cost_payload_omits_costusd_when_not_supplied() -> None:
    part = pa.emit_cost({"input_tokens": 10, "output_tokens": 5}, duration_ms=15)
    payload = pa.parse_cost(part)
    assert payload is not None
    assert "costUsd" not in payload


def test_extension_uri_is_the_canonical_workstacean_uri() -> None:
    # Must match protoWorkstacean's COST_URI for its interceptor to engage.
    assert pa.COST_EXT_URI == "https://proto-labs.ai/a2a/ext/cost-v1"
    assert pa.COST_MIME == "application/vnd.protolabs.cost-v1+json"


def test_record_llm_call_accepts_enriched_signature_when_disabled() -> None:
    import metrics

    # No init() in tests → disabled → no-op, but the signature must accept the
    # new cache/cost kwargs without error.
    metrics.record_llm_call(
        "claude-opus-4-8", "stop", 1.2,
        tokens_input=100, tokens_output=50,
        cache_read=60, cache_creation=10, cost_usd=0.002,
    )
