"""The Codex Responses client — reasoning-item hygiene on the way out (ADR 0097).

The ChatGPT/Codex backend runs with ``store=false``: reasoning state is not kept
server-side, so a replayed reasoning item has to carry its own
``encrypted_content`` blob (which is why the builder asks for
``include=["reasoning.encrypted_content"]``).

langchain-openai's **streaming** Responses path never captures that blob. It reads
the reasoning item at ``response.output_item.added`` — where ``encrypted_content``
is still null — and the terminal ``response.completed`` event rebuilds the full
message but keeps only ``parsed``/usage/``response_metadata`` from it. What DOES
survive into ``additional_kwargs["reasoning"]`` is the item's ``rs_…`` id, and
that id is replayed on the next turn. protoAgent always streams (the Codex backend
mandates it), so this is the only shape it ever produces.

The backend rejects that half-replay:

    400 invalid_encrypted_content — The encrypted content for item rs_… could not
    be verified. Reason: Encrypted content could not be decrypted or parsed.

and because the item rides in ``additional_kwargs`` it is checkpointed, so EVERY
later turn in the thread fails identically — the thread is bricked. Same failure
class as the dangling ``tool_call`` that ``tool_call_repair`` exists to heal.

Two rules, applied to the outbound payload:

- **No blob → drop the item.** An id-only reasoning item is a ghost: with
  ``store=false`` the backend wrote nothing to look up and has nothing to verify.
  Dropping it restores exactly the behaviour ADR 0097's live validation believed
  it already had — no replay, stateless continuity.
- **Blob → keep it, drop the ``id``.** ``encrypted_content`` is self-contained;
  the id only resolves against stored state that ``store=false`` never wrote.

Hermes's Codex adapter arrives at the same two rules independently. Capturing the
blob (so replay actually works) is the follow-up: it needs an
``output_item.done`` handler for reasoning items that langchain-openai lacks.
"""

from __future__ import annotations

import logging
from typing import Any

from graph.llm import _ReasoningChatOpenAI

log = logging.getLogger("protoagent.providers.openai_codex")

# One warning per process: a long thread carries many ghost items and every turn
# re-sends them, so an un-throttled log would drown the turn's real output.
_GHOST_WARNED = False


def sanitize_responses_input(items: Any) -> Any:
    """Drop un-verifiable reasoning items from a Responses ``input`` list.

    Returns ``items`` untouched when it isn't a list (nothing to sanitize) so the
    caller can apply this to any payload shape.
    """
    if not isinstance(items, list):
        return items

    cleaned: list = []
    ghosts = 0
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            cleaned.append(item)
            continue
        blob = item.get("encrypted_content")
        if not (isinstance(blob, str) and blob):
            ghosts += 1
            continue
        cleaned.append({k: v for k, v in item.items() if k != "id"})

    if ghosts:
        global _GHOST_WARNED
        if not _GHOST_WARNED:
            _GHOST_WARNED = True
            log.warning(
                "[openai-codex] dropped %d reasoning item(s) with no encrypted_content "
                "from the Responses input. The streaming path does not capture the blob, "
                "and replaying the item by id alone is what the backend rejects with "
                "400 invalid_encrypted_content. Cross-turn reasoning continuity is off; "
                "the turn itself is unaffected.",
                ghosts,
            )
    return cleaned


class CodexChatOpenAI(_ReasoningChatOpenAI):
    """``ChatOpenAI`` for the Codex backend, with reasoning items sanitized on the
    way out. Every other payload is unchanged — ``input`` exists only on the
    Responses path, so the guard below makes this a no-op anywhere else."""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if isinstance(payload.get("input"), list):
            payload["input"] = sanitize_responses_input(payload["input"])
        return payload
