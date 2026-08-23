"""Per-model token pricing → USD cost (ADR 0006, Slice 1).

``costUsd`` is emitted on the A2A cost-v1 extension so any consumer agrees on the
per-call cost. Best-effort: an unknown model resolves by substring match (gateway
aliases like ``anthropic/claude-opus-4-8``), else falls back to the ``default``
rate. Never raises.

``costUsd`` here bills ``input_tokens`` + ``output_tokens`` at the base rates —
the portion every consumer agrees on. Prompt-cache tokens are captured + emitted
separately (so the cache-hit ratio + savings are *visible*), but folding a
cache discount into ``costUsd`` is deferred until the gateway's cache-token
semantics are validated end-to-end (different gateways disagree on whether
``input_tokens`` already includes cached reads). See ADR 0006.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _per_mtok(inp: float, out: float) -> dict[str, float]:
    """A rate written the way vendors publish it — USD per MILLION tokens — stored
    as USD per token (#3002).

    The table below used to be hand-written in per-token scientific notation, where
    a stale entry is invisible: ``0.000015`` and ``0.000005`` differ by one glyph
    but by 3x in the cost column, and nothing on the line says which vendor number
    it was meant to be. Writing ``_per_mtok(5, 25)`` makes an entry checkable
    against a published price list at a glance, which is what stops the next drift.
    """
    return {"input": inp / 1_000_000, "output": out / 1_000_000}


# USD per MILLION tokens, (input, output) — see `_per_mtok`. Current Anthropic
# first-party rates; partner platforms (Bedrock / Vertex) price separately and are
# not modelled here.
MODEL_RATES: dict[str, dict[str, float]] = {
    # Claude 5 family.
    "claude-fable-5": _per_mtok(10, 50),
    "claude-mythos-5": _per_mtok(10, 50),
    "claude-opus-5": _per_mtok(5, 25),
    "claude-sonnet-5": _per_mtok(3, 15),
    # Claude 4.x. Opus 4.6 dropped to the $5/$25 tier — the pre-4.6 $15/$75 rate
    # that used to sit here billed every Opus turn at 3x (#3002).
    "claude-opus-4-8": _per_mtok(5, 25),
    "claude-opus-4-7": _per_mtok(5, 25),
    "claude-opus-4-6": _per_mtok(5, 25),
    "claude-sonnet-4-6": _per_mtok(3, 15),
    "claude-haiku-4-5": _per_mtok(1, 5),
    "gpt-4o": _per_mtok(2.5, 10),
    "gpt-4o-mini": _per_mtok(0.15, 0.6),
    # protolabs/* are self-hosted vLLM (RTX 6000 Blackwell) — no per-token API
    # spend, so these are a low nominal local-compute estimate (trackable, not
    # billing) rather than the Claude-ish `default` which would overstate cost
    # ~30x. Substring match covers the gateway aliases (protolabs/reasoning →
    # protolabs/smart backend, etc.).
    "protolabs/reasoning": _per_mtok(0.1, 0.4),
    "protolabs/smart": _per_mtok(0.1, 0.4),
    "protolabs/fast": _per_mtok(0.05, 0.2),
    "protolabs/nano": _per_mtok(0.03, 0.1),
    "protolabs": _per_mtok(0.1, 0.4),
    "default": _per_mtok(3, 15),
}

# Models already reported as unpriced. A miss is a per-TURN event, so warning every
# time would flood the log for the whole life of the process; warning once per
# distinct model name is enough to make the gap discoverable (#3002).
_WARNED_UNKNOWN: set[str] = set()


def rate_for(model: str | None) -> dict[str, float]:
    """Resolve the (input, output) rate for a model name.

    Exact match first, then substring (so a gateway alias like
    ``anthropic/claude-opus-4-8`` or ``claude-opus-4-8-20260115`` still
    resolves), else the ``default`` rate.

    A model that reaches ``default`` logs once (#3002). The fallback is the
    dangerous direction: an unrecognised model is silently billed at the mid-tier
    rate, and the models most likely to be missing are the newest and most
    expensive — which is exactly how Opus 5 and Fable 5 came to be undercounted.
    """
    if not model:
        return MODEL_RATES["default"]
    m = str(model).lower()
    if m in MODEL_RATES:
        return MODEL_RATES[m]
    # Longest key first so a more specific key wins over a shorter prefix of it
    # (e.g. "claude-opus-4-8" over a hypothetical "claude-opus").
    for key in sorted((k for k in MODEL_RATES if k != "default"), key=len, reverse=True):
        if key in m:
            return MODEL_RATES[key]
    if m not in _WARNED_UNKNOWN:
        _WARNED_UNKNOWN.add(m)
        log.warning(
            "[pricing] no rate for model %r — billing it at the default rate "
            "($%.2f/$%.2f per Mtok). Cost telemetry for this model is an estimate; "
            "add it to observability/pricing.MODEL_RATES.",
            model,
            MODEL_RATES["default"]["input"] * 1_000_000,
            MODEL_RATES["default"]["output"] * 1_000_000,
        )
    return MODEL_RATES["default"]


def cost_usd(model: str | None, usage: dict) -> float:
    """USD cost for one usage dict ``{input_tokens, output_tokens, ...}``.

    Billed at base input/output rates (fleet-consistent). Returns a value
    rounded to 6 decimals; 0.0 for empty usage.
    """
    rate = rate_for(model)
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    return round(inp * rate["input"] + out * rate["output"], 6)


# Anthropic prompt-cache reads are billed at ~10% of the input rate, so a cached
# read *saves* ~90% of what that token would otherwise cost. This is an estimate
# (the discount varies by provider/tier) used only to prove the cache lever is
# working in dollar terms — not for billing. See ADR 0006 Slice 4.
CACHE_READ_DISCOUNT = 0.9


def cache_read_savings_usd(model: str | None, cache_read_tokens: int) -> float:
    """Estimated USD saved by prompt-cache reads vs. paying full input rate."""
    rate = rate_for(model)
    return round(int(cache_read_tokens or 0) * rate["input"] * CACHE_READ_DISCOUNT, 6)
