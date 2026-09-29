"""Delegate type adapters — a2a / openai / acp (ADR 0025).

Each adapter knows one delegate *type*: the fields it needs (a schema that drives
both the panel form and server-side validation), how to parse a raw config dict
into a ``Delegate``, and how to ``dispatch`` a query to it. A reachability
``probe`` (the panel's "Test" button) lands with the REST API in PR2.

Ported/unified from ORBIS's ``agent/delegate_adapters.py`` — the canonical
protoLabs delegate registry — adapted to protoAgent (the acp adapter reuses the
ADR 0024 ``AcpClient``; the a2a adapter reuses the ``a2a_parse`` A2A parse helpers).
"""

from __future__ import annotations

import json
import logging

# The shared model + base class live in ``base``; the a2a and acp adapters in their own
# modules (#3831). Re-exported here — the long-standing import path for all of them — so
# ``from plugins.delegates.adapters import X`` keeps resolving to the SAME object.
from .a2a import (  # noqa: F401 — re-exported
    _A2A_ERROR_DETAIL_LIMIT,
    _A2A_SUPPORTED_VERSIONS,
    _A2A_TASK_NOT_FOUND,
    _DETACHED_DELEGATION,
    _MAX_WIRE_COST_USD,
    _MAX_WIRE_TOKENS,
    _SHORT_REPLY_CHARS,
    _SHORT_REPLY_MIN_ELAPSED_S,
    A2aAdapter,
    _a2a_auth_hint,
    _a2a_error_detail,
    _a2a_headers,
    _a2a_progress_fingerprint,
    _advertised_a2a_versions,
    _bill_peer_usage,
    _clamp_warned,
    _continuity_credential,
    _fleet_token_available,
    _hub_proxied_slug,
    _is_loopback_url,
    _note_clamped,
    _park_message,
    _peer_usage_row,
    _status_message_text,
    _still_running_message,
    _warn_if_suspiciously_short,
    _wire_int,
    _wire_number,
    mark_delegation_detached,
)
from .acp_adapter import _INCOMPLETE_STOP_REASONS, AcpAdapter, _mark_incomplete  # noqa: F401 — re-exported
from .base import (  # noqa: F401 — re-exported
    _SECRETISH,
    _TOKEN_SPLIT,
    KIND_TIMEOUT,
    KIND_UNREACHABLE,
    Adapter,
    Delegate,
    DelegateError,
    FieldSpec,
    _env_fields,
    _parse_env,
    _secret,
    _timed,
    is_secretish,
)

logger = logging.getLogger("protoagent.plugins.delegates")


class OpenAiAdapter(Adapter):
    type = "openai"
    label = "Model endpoint"
    blurb = "An OpenAI-compatible chat endpoint — ask another model."
    secret_field = "api_key"

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "url",
                "Base URL",
                "text",
                required=True,
                placeholder="https://api.proto-labs.ai/v1",
                help="OpenAI-compatible base URL (the /chat/completions parent).",
            ),
            FieldSpec("model", "Model", "text", required=True, placeholder="protolabs/reasoning"),
            FieldSpec("api_key", "API key", "secret", help="Stored in secrets.yaml (gitignored)."),
            FieldSpec(
                "system_prompt",
                "System prompt",
                "textarea",
                placeholder="Answer thoroughly but concisely.",
                advanced=True,
            ),
            FieldSpec("max_tokens", "Max tokens", "number", default=1024, advanced=True),
            FieldSpec("temperature", "Temperature", "number", default=0.4, advanced=True),
            *_env_fields(),
        ]

    def parse(self, raw: dict) -> Delegate:
        d = Delegate(**self._base(raw))
        d.url = str(raw.get("url", "")).strip()
        d.model = str(raw.get("model", "")).strip()
        if not (d.url and d.model):
            raise DelegateError(f"openai delegate {d.name!r} needs url + model")
        d.api_key = _secret(raw, "api_key", "api_key_env")
        d.system_prompt = str(raw.get("system_prompt", "")).strip()
        try:
            d.max_tokens = int(raw.get("max_tokens") or 1024)
        except (TypeError, ValueError):
            d.max_tokens = 1024
        try:
            d.temperature = float(raw.get("temperature") if raw.get("temperature") is not None else 0.4)
        except (TypeError, ValueError):
            d.temperature = 0.4
        _parse_env(raw, d)
        return d

    async def dispatch(
        self,
        d: Delegate,
        query: str,
        *,
        timeout: float | None = None,
        item_id: str | None = None,
        resume_task_id: str | None = None,
    ) -> str:
        import httpx

        messages = []
        if d.system_prompt:
            messages.append({"role": "system", "content": d.system_prompt})
        messages.append({"role": "user", "content": query})
        headers = {"Content-Type": "application/json"}
        if d.api_key:
            headers["Authorization"] = f"Bearer {d.api_key}"
        url = d.url.rstrip("/") + "/chat/completions"
        payload = {"model": d.model, "messages": messages, "max_tokens": d.max_tokens, "temperature": d.temperature}
        # Name the delegate and the CAUSE, the way the a2a leg does. These strings land in
        # a tool result: an unattributed `HTTP 401` tells an agent fanned out across
        # several delegates neither which one failed nor whether retrying is pointless.
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            try:
                r = await client.post(url, json=payload, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise DelegateError(
                    f"delegate {d.name!r} unreachable at {url} ({type(exc).__name__})",
                    kind=KIND_UNREACHABLE,
                ) from exc
            except httpx.TimeoutException as exc:
                raise DelegateError(f"delegate {d.name!r} timed out contacting {url}") from exc
            except httpx.HTTPError as exc:
                raise DelegateError(f"delegate {d.name!r} transport error: {str(exc)[:160]}") from exc
            if r.status_code >= 400:
                raise DelegateError(f"delegate {d.name!r} HTTP {r.status_code} from {url}: {r.text[:400]}")
            try:
                data = r.json()
            except ValueError as exc:
                raise DelegateError(f"delegate {d.name!r} returned non-JSON from {url}: {r.text[:200]!r}") from exc
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            # An OpenAI-compatible endpoint that refuses in-band answers 200 with an
            # `error` object instead of `choices`; show it rather than the KeyError.
            inline = data.get("error") if isinstance(data, dict) else None
            if inline:
                raise DelegateError(f"delegate {d.name!r} endpoint error: {json.dumps(inline, default=str)[:400]}")
            raise DelegateError(f"delegate {d.name!r} unexpected response shape: {exc}")

    async def probe(self, d: Delegate) -> dict:
        import httpx

        headers = {"Authorization": f"Bearer {d.api_key}"} if d.api_key else {}
        url = d.url.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r, ms = await _timed(client.get(url, headers=headers))
            if r.status_code >= 400:
                return {"ok": False, "latency_ms": ms, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
            return {"ok": True, "latency_ms": ms, "detail": "endpoint reachable"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}


ADAPTERS: dict[str, Adapter] = {a.type: a for a in (A2aAdapter(), OpenAiAdapter(), AcpAdapter())}


def delegate_types() -> list[dict]:
    """Type list + field schemas — drives the panel (PR3) and /delegate-types (PR2)."""
    return [
        {"type": a.type, "label": a.label, "blurb": a.blurb, "fields": [f.as_dict() for f in a.config_schema()]}
        for a in ADAPTERS.values()
    ]
