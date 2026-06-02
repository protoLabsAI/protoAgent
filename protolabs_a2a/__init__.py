"""protolabs-a2a — protoLabs A2A 1.0 conventions on top of ``a2a-sdk``.

A THIN conventions layer: ``a2a-sdk`` owns all protocol mechanics (JSON-RPC,
SSE, task lifecycle, push delivery). This package owns only what is specific to
the protoLabs fleet so every agent (in-process Ava, roxy, ORBIS, pwnDeck,
protoAgent) interoperates byte-for-byte:

  - ``extensions`` — the four custom DataPart conventions (cost / confidence /
    worldstate-delta / tool-call): MIME + card-URI constants, payload types,
    and ``emit_*`` / ``parse_*`` helpers.
  - ``parts`` — build / read the A2A 1.0 member-discriminated Part shape.
  - ``agent_card`` — ``build_agent_card`` applying provider, JSONRPC interface,
    extension declarations, and auth schemes.
  - ``auth`` — the ``apiKey`` / ``bearer`` security-scheme builders.
"""

from __future__ import annotations

from .agent_card import (
    PROVIDER_ORGANIZATION,
    PROVIDER_URL,
    build_agent_card,
    jsonrpc_interface,
    protolabs_provider,
)
from .auth import (
    API_KEY_HEADER,
    API_KEY_SCHEME_NAME,
    BEARER_SCHEME_NAME,
    api_key_scheme,
    bearer_scheme,
    security_requirements,
    security_schemes,
)
from .extensions import (
    ALL_EXTENSION_URIS,
    CONFIDENCE_EXT_URI,
    CONFIDENCE_MIME,
    COST_EXT_URI,
    COST_MIME,
    TOOL_CALL_EXT_URI,
    TOOL_CALL_MIME,
    WORLDSTATE_DELTA_EXT_URI,
    WORLDSTATE_DELTA_MIME,
    ConfidencePayload,
    CostPayload,
    CostUsage,
    ToolCallPayload,
    WorldstateDelta,
    WorldstateDeltaPayload,
    emit_confidence,
    emit_cost,
    emit_tool_call,
    emit_worldstate_delta,
    parse_confidence,
    parse_cost,
    parse_tool_call,
    parse_worldstate_delta,
)
from .parts import (
    DATA_MEDIA_TYPE,
    MIME_KEY,
    data_part,
    part_mime,
    read_data,
    read_text,
    text_part,
)

__all__ = [
    # parts
    "text_part",
    "data_part",
    "read_text",
    "read_data",
    "part_mime",
    "MIME_KEY",
    "DATA_MEDIA_TYPE",
    # extensions — MIME constants
    "COST_MIME",
    "CONFIDENCE_MIME",
    "WORLDSTATE_DELTA_MIME",
    "TOOL_CALL_MIME",
    # extensions — card URIs
    "COST_EXT_URI",
    "CONFIDENCE_EXT_URI",
    "WORLDSTATE_DELTA_EXT_URI",
    "TOOL_CALL_EXT_URI",
    "ALL_EXTENSION_URIS",
    # extensions — emit
    "emit_cost",
    "emit_confidence",
    "emit_worldstate_delta",
    "emit_tool_call",
    # extensions — parse
    "parse_cost",
    "parse_confidence",
    "parse_worldstate_delta",
    "parse_tool_call",
    # extensions — payload types
    "CostUsage",
    "CostPayload",
    "ConfidencePayload",
    "WorldstateDelta",
    "WorldstateDeltaPayload",
    "ToolCallPayload",
    # agent card
    "build_agent_card",
    "protolabs_provider",
    "jsonrpc_interface",
    "PROVIDER_ORGANIZATION",
    "PROVIDER_URL",
    # auth
    "security_schemes",
    "security_requirements",
    "api_key_scheme",
    "bearer_scheme",
    "API_KEY_SCHEME_NAME",
    "BEARER_SCHEME_NAME",
    "API_KEY_HEADER",
]
