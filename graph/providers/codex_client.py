"""The Codex Responses client — encrypted-reasoning capture + replay (ADR 0097).

The ChatGPT/Codex backend runs with ``store=false``: reasoning state is not kept
server-side, so cross-turn reasoning continuity depends on threading each item's
own ``encrypted_content`` blob back through history. #3199 established the first
half of the contract — never send an item the backend cannot verify. This module
now also delivers the other half: capture the blob, and only replay it where it
can actually be decrypted.

**Why capture needs code at all.** ``encrypted_content`` reaches a stream in
exactly one event — ``response.output_item.done`` for the reasoning item. The
``.added`` event that opens the item has the field still null, and the terminal
``response.completed`` event rebuilds the full message but keeps only
``parsed``/usage/``response_metadata``. langchain-openai **≥ 1.6** handles that
event; **< 1.6** drops it on the floor, so the blob was never captured at all.
``pyproject`` floors langchain-openai at 1.0, so both are live.

`_install_reasoning_capture` therefore does two things at that one event: it
synthesizes the content-block delta when the installed langchain didn't (merging
onto the in-flight reasoning block by ``index``, the way every other streamed
block merges), and it stamps the issuer either way. On a modern langchain the
first half is a no-op — the wrapper only ever adds what is missing.

It is installed on the module-level converter (there is no instance seam) but is
**inert unless ``_CAPTURE_ISSUER`` is set**, and only this module's client sets
it, for the duration of its own stream. Any other ``ChatOpenAI`` in the process —
a gateway client, a plain Responses user — is untouched.

**Why the issuer stamp.** ``encrypted_content`` is sealed to the endpoint *and
account* that minted it; replaying a blob anywhere else is a hard
``400 invalid_encrypted_content`` that, once checkpointed, bricks the thread. That
is not hypothetical here: protoAgent lets every slot name its own connection
(``gateway:`` / ``anthropic-oauth:`` / ``openai-codex:``), lets each chat tab
override the model per turn, and retries a failed turn against the fallback chain.
So each captured item carries a fingerprint of its issuer, and replay drops items
minted elsewhere instead of poisoning the request. Hermes's Codex adapter reaches
the same design (`_classify_responses_issuer`); the stamp is a salted digest so a
checkpoint never stores a raw account id.

Outbound rules, applied to every Responses ``input``:

- **No blob → drop.** An id-only reasoning item is a ghost: with ``store=false``
  the backend wrote nothing to look up and has nothing to verify (#3199).
- **Foreign issuer → drop.** The current endpoint cannot decrypt it. Unstamped
  items (written before this landed) are still replayed.
- **Otherwise keep the blob, drop the ``id``.** An item id only resolves against
  stored state that ``store=false`` never wrote; the blob is self-contained.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
from typing import Any

from graph.llm import _ReasoningChatOpenAI

log = logging.getLogger("protoagent.providers.openai_codex")

# Our own key on a captured reasoning block. Never goes on the wire — the
# sanitizer below strips every key with this prefix on the way out.
_PRIVATE_PREFIX = "_protoagent_"
ISSUER_KEY = f"{_PRIVATE_PREFIX}issuer"

# Set by this module's client for the duration of ITS stream; the capture wrapper
# is a no-op for every other caller of the shared converter.
_CAPTURE_ISSUER: contextvars.ContextVar[str] = contextvars.ContextVar("protoagent_codex_issuer", default="")

# One warning per process per cause: a long thread carries many affected items and
# re-sends them every turn, so un-throttled logs would drown the turn's output.
_WARNED: set[str] = set()


def issuer_fingerprint(base_url: str, account_id: str) -> str:
    """A stable, non-identifying id for the endpoint+account that mints blobs.

    Digested rather than stored raw: this value is checkpointed alongside the
    conversation, and the account id has no business living in that file.
    """
    raw = f"{(base_url or '').strip().rstrip('/')}\x00{(account_id or '').strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning(message, *args)


def sanitize_responses_input(items: Any, *, issuer: str = "") -> Any:
    """Drop reasoning items this endpoint cannot verify; strip private keys.

    ``items`` is returned untouched when it isn't a list, so a caller can apply
    this to any payload shape.
    """
    if not isinstance(items, list):
        return items

    cleaned: list = []
    ghosts = 0
    foreign = 0
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            cleaned.append(item)
            continue

        blob = item.get("encrypted_content")
        if not (isinstance(blob, str) and blob):
            ghosts += 1
            continue

        stamped = item.get(ISSUER_KEY)
        if stamped and issuer and stamped != issuer:
            foreign += 1
            continue

        # `id` is unresolvable under store=false; private keys are ours, not the
        # API's. Everything else (summary, the blob itself) replays as-is.
        cleaned.append({k: v for k, v in item.items() if k != "id" and not k.startswith(_PRIVATE_PREFIX)})

    if ghosts:
        _warn_once(
            "ghost",
            "[openai-codex] dropped %d reasoning item(s) with no encrypted_content from "
            "the Responses input — replaying one by id alone is what the backend rejects "
            "with 400 invalid_encrypted_content. The turn itself is unaffected.",
            ghosts,
        )
    if foreign:
        _warn_once(
            "foreign",
            "[openai-codex] dropped %d reasoning item(s) minted by a different endpoint or "
            "account — encrypted_content is sealed to its issuer, so this endpoint cannot "
            "decrypt them. This is normal after a mid-conversation model swap or a re-login; "
            "cross-turn reasoning continuity restarts from here.",
            foreign,
        )
    return cleaned


def _install_reasoning_capture() -> None:
    """Teach the shared Responses chunk converter to surface ``encrypted_content``.

    Idempotent, and gated on ``_CAPTURE_ISSUER`` so it changes nothing for any
    other client in the process. Delegates to the original for every event —
    it only ever ADDS a chunk where the original produced none.
    """
    from langchain_openai.chat_models import base as lc_base

    original = lc_base._convert_responses_chunk_to_generation_chunk
    if getattr(original, "_protoagent_reasoning_capture", False):
        return

    def _capture(chunk, current_index, current_output_index, current_sub_index, *args, **kwargs):
        result = original(chunk, current_index, current_output_index, current_sub_index, *args, **kwargs)
        issuer = _CAPTURE_ISSUER.get()
        if not issuer or getattr(chunk, "type", "") != "response.output_item.done":
            return result
        item = getattr(chunk, "item", None)
        if getattr(item, "type", "") != "reasoning":
            return result
        blob = getattr(item, "encrypted_content", None)
        if not (isinstance(blob, str) and blob):
            return result

        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        generation = result[3]
        if generation is not None:
            # langchain >= 1.6 already surfaces the blob; only the stamp is ours.
            content = [
                {**b, ISSUER_KEY: issuer} if isinstance(b, dict) and b.get("encrypted_content") else b
                for b in generation.message.content
            ]
            message = generation.message.model_copy(update={"content": content})
            return (result[0], result[1], result[2], ChatGenerationChunk(message=message))

        # langchain < 1.6 drops this event, so the blob has to be re-emitted.
        # `index` merges it onto the block the item's `.added` event opened; the
        # summary deltas in between never advance it. `type` is deliberately
        # ABSENT — merge_dicts concatenates two equal strings for any key but
        # `id`, so re-sending it would yield "reasoningreasoning". `id` IS safe
        # (equal values are skipped) and keeps the merge off a neighbouring block.
        block = {
            "index": current_index,
            "id": getattr(item, "id", None),
            "encrypted_content": blob,
            ISSUER_KEY: issuer,
        }
        return (
            current_index,
            current_output_index,
            current_sub_index,
            ChatGenerationChunk(message=AIMessageChunk(content=[block])),
        )

    _capture._protoagent_reasoning_capture = True  # type: ignore[attr-defined]
    lc_base._convert_responses_chunk_to_generation_chunk = _capture


class CodexChatOpenAI(_ReasoningChatOpenAI):
    """``ChatOpenAI`` for the Codex backend: captures encrypted reasoning on the
    way in, and replays only what this endpoint can verify on the way out."""

    @property
    def _issuer(self) -> str:
        return getattr(self, "_protoagent_issuer_fp", "") or ""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # `input` exists only on the Responses path, so this is a no-op elsewhere.
        if isinstance(payload.get("input"), list):
            payload["input"] = sanitize_responses_input(payload["input"], issuer=self._issuer)
        return payload

    # Arming happens on `_stream`/`_astream`, NOT on `_stream_responses`.
    # `ChatOpenAI._stream` routes with `super()._stream_responses(...)` — an
    # explicit `super(ChatOpenAI, self)` bind that skips right past this subclass,
    # so an override there is dead code and the capture would never arm in
    # production while every unit test that sets the contextvar itself still
    # passes. `_stream`/`_astream` are dispatched on the instance, so they are the
    # seam that actually runs.
    def _stream(self, *args, **kwargs):
        token = _CAPTURE_ISSUER.set(self._issuer)
        try:
            yield from super()._stream(*args, **kwargs)
        finally:
            _CAPTURE_ISSUER.reset(token)

    async def _astream(self, *args, **kwargs):
        token = _CAPTURE_ISSUER.set(self._issuer)
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        finally:
            _CAPTURE_ISSUER.reset(token)


def build_codex_client(*, issuer: str, **kwargs: Any) -> CodexChatOpenAI:
    """A ``CodexChatOpenAI`` stamped with the issuer of the endpoint it talks to.

    The stamp rides as a private attribute rather than a pydantic field — the same
    way ``graph.providers.identity`` tags routing identity — so the client's
    serialized shape is unchanged.
    """
    _install_reasoning_capture()
    client = CodexChatOpenAI(**kwargs)
    object.__setattr__(client, "_protoagent_issuer_fp", issuer)
    return client
