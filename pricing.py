"""Per-model token pricing → USD cost (ADR 0006, Slice 1).

Rates mirror the structure + overlapping values of Workstacean's ``MODEL_RATES``
(``protoWorkstacean/lib/types/budget.ts``) so protoAgent's emitted ``costUsd``
agrees with the fleet's fallback computation. Cost is best-effort: an unknown
model resolves by substring match (gateway aliases like
``anthropic/claude-opus-4-8``), else falls back to the ``default`` rate. Never
raises.

``costUsd`` here bills ``input_tokens`` + ``output_tokens`` at the base rates —
the portion every consumer agrees on. Prompt-cache tokens are captured + emitted
separately (so the cache-hit ratio + savings are *visible*), but folding a
cache discount into ``costUsd`` is deferred until the gateway's cache-token
semantics are validated end-to-end (different gateways disagree on whether
``input_tokens`` already includes cached reads). See ADR 0006.
"""

from __future__ import annotations

# USD per token, (input, output). Keep in sync with Workstacean MODEL_RATES.
MODEL_RATES: dict[str, dict[str, float]] = {
    "claude-opus-4-8":           {"input": 0.000015,   "output": 0.000075},
    "claude-opus-4-6":           {"input": 0.000015,   "output": 0.000075},
    "claude-sonnet-4-6":         {"input": 0.000003,   "output": 0.000015},
    "claude-haiku-4-5":          {"input": 0.00000025, "output": 0.00000125},
    "claude-haiku-4-5-20251001": {"input": 0.00000025, "output": 0.00000125},
    "gpt-4o":                    {"input": 0.0000025,  "output": 0.00001},
    "gpt-4o-mini":               {"input": 0.00000015, "output": 0.0000006},
    "default":                   {"input": 0.000003,   "output": 0.000015},
}


def rate_for(model: str | None) -> dict[str, float]:
    """Resolve the (input, output) rate for a model name.

    Exact match first, then substring (so a gateway alias like
    ``anthropic/claude-opus-4-8`` or ``claude-opus-4-8-20260115`` still
    resolves), else the ``default`` rate.
    """
    if not model:
        return MODEL_RATES["default"]
    m = str(model).lower()
    if m in MODEL_RATES:
        return MODEL_RATES[m]
    # Longest key first so "claude-haiku-4-5-20251001" wins over a shorter key.
    for key in sorted((k for k in MODEL_RATES if k != "default"), key=len, reverse=True):
        if key in m:
            return MODEL_RATES[key]
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
