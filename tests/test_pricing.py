"""Tests for pricing.py (ADR 0006 Slice 1 — per-model token → USD cost)."""

from __future__ import annotations

import pricing


def test_rate_for_exact_match() -> None:
    assert pricing.rate_for("claude-opus-4-8")["input"] == 0.000015


def test_rate_for_substring_alias() -> None:
    # Gateway aliases / dated suffixes resolve by substring.
    assert pricing.rate_for("anthropic/claude-sonnet-4-6")["output"] == 0.000015
    assert pricing.rate_for("claude-haiku-4-5-20251001")["input"] == 0.00000025


def test_rate_for_unknown_falls_back_to_default() -> None:
    assert pricing.rate_for("some-future-model") == pricing.MODEL_RATES["default"]
    assert pricing.rate_for(None) == pricing.MODEL_RATES["default"]


def test_cost_usd_base_rates() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 500}
    # opus: 1000*0.000015 + 500*0.000075 = 0.015 + 0.0375 = 0.0525
    assert pricing.cost_usd("claude-opus-4-8", usage) == 0.0525


def test_cost_usd_empty_usage_is_zero() -> None:
    assert pricing.cost_usd("claude-opus-4-8", {}) == 0.0


def test_cost_usd_handles_none_and_missing_fields() -> None:
    # Robust to partial usage dicts (no crash, sensible number).
    assert pricing.cost_usd("gpt-4o", {"input_tokens": 100}) == round(100 * 0.0000025, 6)


def test_rate_for_protolabs_gateway_models() -> None:
    # Self-hosted protolabs/* (vLLM) — low nominal local-compute estimate, not the
    # Claude-ish default (which would overstate ~30x). Aliases resolve by substring.
    assert pricing.rate_for("protolabs/reasoning") == pricing.MODEL_RATES["protolabs/reasoning"]
    assert pricing.rate_for("protolabs/fast")["input"] < pricing.MODEL_RATES["default"]["input"]
