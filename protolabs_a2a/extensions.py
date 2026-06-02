"""protoLabs A2A custom extensions — the four fleet-wide DataPart conventions.

Each extension is a ``(MIME, card-extension-URI, payload)`` triple. The MIME is
the ``metadata.mimeType`` discriminator on a custom DataPart (see ``parts.py``);
the URI is what the agent card declares in ``capabilities.extensions[]`` so a
consumer knows to extract the part; the payload is the JSON object under the
part's ``content.value``.

Mirrors the TS reference layer (``@protolabs/a2a``) byte-for-byte so the hub and
every agent (in-process Ava, roxy, ORBIS, pwnDeck, protoAgent) interoperate.

The four extensions
───────────────────
  cost-v1              token usage + duration + cost on a terminal task
  confidence-v1        agent self-reported confidence on a terminal task
  worldstate-delta-v1  observed shared-state mutations on a terminal task
  tool-call-v1         per-tool progress event on a status frame

``emit_*`` returns a wire-shaped DataPart dict (via ``parts.data_part``).
``parse_*`` takes a part dict and returns the typed payload iff the part's MIME
matches that extension, else None — so a consumer can route ``parts`` by MIME.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from .parts import data_part, read_data

# ── MIME types (the metadata.mimeType discriminator) ──────────────────────────
COST_MIME = "application/vnd.protolabs.cost-v1+json"
CONFIDENCE_MIME = "application/vnd.protolabs.confidence-v1+json"
WORLDSTATE_DELTA_MIME = "application/vnd.protolabs.worldstate-delta-v1+json"
TOOL_CALL_MIME = "application/vnd.protolabs.tool-call-v1+json"

# ── Card extension URIs (capabilities.extensions[].uri) ───────────────────────
COST_EXT_URI = "https://proto-labs.ai/a2a/ext/cost-v1"
CONFIDENCE_EXT_URI = "https://proto-labs.ai/a2a/ext/confidence-v1"
WORLDSTATE_DELTA_EXT_URI = "https://proto-labs.ai/a2a/ext/worldstate-delta-v1"
TOOL_CALL_EXT_URI = "https://proto-labs.ai/a2a/ext/tool-call-v1"


# ── Payload shapes ────────────────────────────────────────────────────────────


class CostUsage(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


class CostPayload(TypedDict, total=False):
    usage: CostUsage
    durationMs: int
    costUsd: float
    success: bool


class ConfidencePayload(TypedDict, total=False):
    confidence: float
    explanation: str
    success: bool


class WorldstateDelta(TypedDict):
    domain: str
    path: str
    op: Literal["set", "inc", "push"]
    value: Any


class WorldstateDeltaPayload(TypedDict):
    deltas: list[WorldstateDelta]


class ToolCallPayload(TypedDict, total=False):
    toolCallId: str
    name: str
    phase: Literal["started", "completed", "failed"]
    args: Any
    result: Any
    error: str


# ── cost-v1 ───────────────────────────────────────────────────────────────────


def emit_cost(
    usage: CostUsage,
    *,
    duration_ms: int | None = None,
    cost_usd: float | None = None,
    success: bool | None = None,
) -> dict[str, Any]:
    """Build a cost-v1 DataPart. ``usage`` token counts are required; duration,
    cost, and success are included only when provided."""
    payload: CostPayload = {"usage": usage}
    if duration_ms is not None:
        payload["durationMs"] = duration_ms
    if cost_usd is not None:
        payload["costUsd"] = cost_usd
    if success is not None:
        payload["success"] = success
    return data_part(payload, COST_MIME)


def parse_cost(part: dict[str, Any]) -> CostPayload | None:
    mime, payload = read_data(part)
    return payload if mime == COST_MIME and isinstance(payload, dict) else None


# ── confidence-v1 ─────────────────────────────────────────────────────────────


def emit_confidence(
    confidence: float,
    *,
    explanation: str | None = None,
    success: bool | None = None,
) -> dict[str, Any]:
    """Build a confidence-v1 DataPart."""
    payload: ConfidencePayload = {"confidence": confidence}
    if explanation:
        payload["explanation"] = explanation
    if success is not None:
        payload["success"] = success
    return data_part(payload, CONFIDENCE_MIME)


def parse_confidence(part: dict[str, Any]) -> ConfidencePayload | None:
    mime, payload = read_data(part)
    return payload if mime == CONFIDENCE_MIME and isinstance(payload, dict) else None


# ── worldstate-delta-v1 ───────────────────────────────────────────────────────


def emit_worldstate_delta(deltas: list[WorldstateDelta]) -> dict[str, Any]:
    """Build a worldstate-delta-v1 DataPart wrapping ``deltas``."""
    return data_part({"deltas": list(deltas)}, WORLDSTATE_DELTA_MIME)


def parse_worldstate_delta(part: dict[str, Any]) -> WorldstateDeltaPayload | None:
    mime, payload = read_data(part)
    return payload if mime == WORLDSTATE_DELTA_MIME and isinstance(payload, dict) else None


# ── tool-call-v1 ──────────────────────────────────────────────────────────────


def emit_tool_call(
    tool_call_id: str,
    name: str,
    phase: Literal["started", "completed", "failed"],
    *,
    args: Any = None,
    result: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build a tool-call-v1 DataPart for a per-tool progress event."""
    payload: ToolCallPayload = {"toolCallId": tool_call_id, "name": name, "phase": phase}
    if args is not None:
        payload["args"] = args
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    return data_part(payload, TOOL_CALL_MIME)


def parse_tool_call(part: dict[str, Any]) -> ToolCallPayload | None:
    mime, payload = read_data(part)
    return payload if mime == TOOL_CALL_MIME and isinstance(payload, dict) else None


# Convenience: all four card-extension URIs, in the canonical order.
ALL_EXTENSION_URIS = (
    COST_EXT_URI,
    CONFIDENCE_EXT_URI,
    WORLDSTATE_DELTA_EXT_URI,
    TOOL_CALL_EXT_URI,
)
