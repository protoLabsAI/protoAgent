"""Tests for pricing.py (ADR 0006 Slice 1 — per-model token → USD cost)."""

from __future__ import annotations

import logging

import pytest

from observability import pricing


def test_rate_for_exact_match() -> None:
    assert pricing.rate_for("claude-opus-4-8")["input"] == 0.000005


def test_rate_for_substring_alias() -> None:
    # Gateway aliases / dated suffixes resolve by substring.
    assert pricing.rate_for("anthropic/claude-sonnet-4-6")["output"] == 0.000015
    assert pricing.rate_for("claude-haiku-4-5-20251001")["input"] == 0.000001


def test_rate_for_unknown_falls_back_to_default() -> None:
    assert pricing.rate_for("some-future-model") == pricing.MODEL_RATES["default"]
    assert pricing.rate_for(None) == pricing.MODEL_RATES["default"]


# The shipped rate for every model we actually route to, in the units vendors
# publish (USD per MILLION tokens). This is the guard #3002 was missing: the table
# had drifted to pre-4.6 Opus pricing (3x over), a quartered Haiku rate, and no
# entry at all for the Claude 5 family — which silently fell through to `default`.
# A wrong number here is a wrong number in every cost column we render, so the
# rates are pinned as a checked fact rather than left as a comment.
CURRENT_RATES_PER_MTOK = {
    "claude-fable-5": (10, 50),
    "claude-mythos-5": (10, 50),
    "claude-opus-5": (5, 25),
    "claude-opus-4-8": (5, 25),
    "claude-opus-4-7": (5, 25),
    "claude-opus-4-6": (5, 25),
    "claude-sonnet-5": (3, 15),
    "claude-sonnet-4-6": (3, 15),
    "claude-haiku-4-5": (1, 5),
}


@pytest.mark.parametrize(("model", "expected"), sorted(CURRENT_RATES_PER_MTOK.items()))
def test_current_claude_rates_are_correct(model: str, expected: tuple[float, float]) -> None:
    rate = pricing.rate_for(model)
    assert (rate["input"] * 1_000_000, rate["output"] * 1_000_000) == expected


@pytest.mark.parametrize("model", sorted(CURRENT_RATES_PER_MTOK))
def test_current_models_never_resolve_via_the_default(model: str) -> None:
    # A model that lands on `default` is billed at the mid-tier rate with no signal.
    # `claude-sonnet-5` used to pass a naive cost assertion for exactly this reason:
    # it matched no key and `default` happens to be Sonnet-priced (#3002).
    assert pricing.rate_for(model) is not pricing.MODEL_RATES["default"]


def test_unknown_model_warns_once(caplog) -> None:
    pricing._WARNED_UNKNOWN.discard("mystery-model-9")
    with caplog.at_level(logging.WARNING, logger="observability.pricing"):
        pricing.rate_for("mystery-model-9")
        pricing.rate_for("mystery-model-9")
    hits = [r for r in caplog.records if "mystery-model-9" in r.getMessage()]
    assert len(hits) == 1, "an unpriced model must be reported, but only once per process"


def test_cost_usd_base_rates() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 500}
    # opus: 1000*0.000005 + 500*0.000025 = 0.005 + 0.0125 = 0.0175
    assert pricing.cost_usd("claude-opus-4-8", usage) == 0.0175


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


# ── Prompt-cache pricing (#3003) ──────────────────────────────────────────────
# `cost_usd` takes LangChain-shaped usage, where `input_tokens` is "the sum of all
# input token types" and the cache counts are SUBSETS of it — not additions.


def test_cached_reads_are_billed_at_a_tenth_not_full_price():
    rate = pricing.rate_for("claude-opus-5")["input"]
    usage = {"input_tokens": 1000, "output_tokens": 0, "cache_read_input_tokens": 900}
    # 100 uncached at full rate + 900 cached at 10%.
    assert pricing.cost_usd("claude-opus-5", usage) == pytest.approx(round(100 * rate + 900 * rate * 0.1, 6))
    # The whole point: a 90%-cached turn is not billed like an uncached one.
    assert pricing.cost_usd("claude-opus-5", usage) < 0.25 * (1000 * rate)


def test_cache_writes_cost_more_than_plain_input():
    rate = pricing.rate_for("claude-opus-5")["input"]
    usage = {"input_tokens": 1000, "output_tokens": 0, "cache_creation_input_tokens": 1000}
    assert pricing.cost_usd("claude-opus-5", usage) == pytest.approx(round(1000 * rate * 1.25, 6))


def test_langchain_nested_cache_details_are_understood():
    # The producers hand over `usage_metadata` directly, whose cache counts are
    # nested under `input_token_details` rather than the flat telemetry-row names.
    flat = {"input_tokens": 1000, "output_tokens": 10, "cache_read_input_tokens": 800}
    nested = {"input_tokens": 1000, "output_tokens": 10, "input_token_details": {"cache_read": 800}}
    assert pricing.cost_usd("claude-opus-5", nested) == pricing.cost_usd("claude-opus-5", flat)


def test_uncached_usage_is_unchanged_by_the_cache_split():
    # A turn with no cache activity must cost exactly what it always did.
    rate = pricing.rate_for("claude-opus-5")
    usage = {"input_tokens": 1000, "output_tokens": 500}
    assert pricing.cost_usd("claude-opus-5", usage) == pytest.approx(
        round(1000 * rate["input"] + 500 * rate["output"], 6)
    )


def test_cache_counts_exceeding_input_cannot_refund_the_turn():
    # Defensive: a provider that reports cache counts NOT included in input_tokens
    # (against the documented contract) must not drive the full-price component
    # negative and hand back a credit.
    usage = {"input_tokens": 100, "output_tokens": 0, "cache_read_input_tokens": 5000}
    assert pricing.cost_usd("claude-opus-5", usage) > 0
