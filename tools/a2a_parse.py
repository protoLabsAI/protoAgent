"""A2A 1.0 response parse helpers — reading a peer's reply off the wire.

This module used to ship the ``peer_consult`` / ``peer_list`` tools (env-var peer
federation). Those were **retired**: ``delegate_to`` over the unified delegate
registry (ADR 0025, ``plugins/delegates``) is the one federation tool — same A2A
consult alongside openai/acp delegates, with a console panel. What remains here
are the pure parse helpers that read an A2A 1.0 ``SendMessage`` / ``GetTask``
result: the reply text, the task state, and (since #3016) the peer's own cost-v1
telemetry. They live in ``tools/`` rather than in the delegates plugin because
they describe the WIRE, not the delegate registry — core surfaces that read a
peer's result must not have to import from ``plugins/``.
"""

from __future__ import annotations

import protolabs_a2a as pa

# The name a peer's spend travels under once it has been read off the wire (#3016).
# cost-v1 carries no model field, so the delegates adapter tags the usage row it
# builds with ``peer:<delegate>`` — a MARKER, not a model name. It rides the turn's
# ``models`` list into the stored telemetry row, which is the only durable trace a
# delegation leaves. Defined here, in the lowest layer every end can import: the
# adapter (``plugins/delegates``) writes it, and the readers below recognise it so a
# marker never becomes a turn's primary ``model``.
PEER_MODEL_PREFIX = "peer:"


def drop_peer_markers(models) -> list[str]:
    """A turn's model list with the ``peer:`` markers removed (#3016).

    Every field that names *the model that ran this turn* has to pick from this rather
    than from the raw list — the telemetry row's ``model`` column and the ``turn.usage``
    bus event (``server.turn_telemetry``), and the fleet trace export's ``meta.model``
    (``observability.trace_export``), which the lab consumes as the teacher model. A
    marker names an agent, so a per-model breakdown or a training row that says
    ``peer:orbis`` is simply wrong. The raw list keeps its markers either way: that CSV
    is where a delegation's spend stays legible.

    Usually a no-op, because the lead's own call precedes any delegation. It earns its
    keep in the edge that isn't: a provider reporting no usage at all yields no frame
    for the lead, and the marker then leads the list.
    """
    return [m for m in (models or []) if not str(m).startswith(PEER_MODEL_PREFIX)]


def _task_of(result) -> dict:
    """The task out of an A2A 1.0 result, whichever envelope it arrived in.

    ``SendMessage`` answers ``{"task": …}`` while ``GetTask``'s result IS the bare
    task, and the adapter hands both shapes to the same readers — so the tolerance
    lives here once instead of in each of them.
    """
    if not isinstance(result, dict):
        return {}
    task = result.get("task", result)
    return task if isinstance(task, dict) else {}


def _extract_text(result) -> str | None:
    """Pull text out of an A2A 1.0 result — a ``{"task": ...}`` envelope (the
    ``SendMessage`` / ``GetTask`` response) or a bare Message. Tolerant of parts
    with or without an explicit ``kind`` tag (1.0 text parts carry just ``text``).

    Text parts are concatenated with the EMPTY string, and across EVERY text-bearing
    artifact rather than only the first (#3085). A peer that streams its reply one
    part — or one artifact — per delta was otherwise read two ways wrong: the tail
    beyond the first artifact was dropped, and a newline was spliced between the parts
    that survived. A streamed delta already carries its own leading whitespace
    (``"Let"`` + ``" me"`` reassembles to ``"Let me"``), so joining parts on a newline
    broke every word that fell on a chunk boundary and truncated the answer to
    whatever reached the first artifact. An empty-string join over all artifacts
    rebuilds the reply exactly as it was streamed; a peer that means a paragraph break
    still sends that newline inside a part's own text, so nothing real is lost."""
    task = _task_of(result)
    chunks = [
        p.get("text", "")
        for art in task.get("artifacts") or []
        for p in art.get("parts", [])
        if p.get("text")
    ]
    if any(chunks):
        return "".join(chunks)
    msg = (task.get("status") or {}).get("message") or {}
    parts = [p.get("text", "") for p in (msg.get("parts") or []) if p.get("text")]
    text = "".join(parts)
    return text or None


def _extract_cost(result) -> dict | None:
    """The peer's cost-v1 payload off an A2A 1.0 result, or ``None`` (#3016).

    A protoAgent peer measures the turn it just ran for us and ships the numbers
    back: ``a2a_impl/executor.py::_terminal_parts`` merges a cost-v1 fragment into
    the TERMINAL ARTIFACT's metadata map, keyed by the extension URI (protolabs-a2a
    0.3.0 moved the payload off DataParts). The terminal status message's metadata
    is the extension's other permitted home, so it is the fallback.

    Artifacts are scanned LAST-wins, deliberately diverging from ``_extract_text``
    above, which CONCATENATES every text-bearing artifact. For a protoAgent peer the
    two agree — its whole task carries one artifact (``{task_id}-answer``, replaced in
    place on every leg). They only differ for a peer that appends a fresh artifact per
    leg: there we want every artifact's WORDS but only the NEWEST telemetry, since
    billing a stale first leg would charge this turn for spend an earlier dispatch
    already caused.
    """
    task = _task_of(result)
    found: dict | None = None
    for art in task.get("artifacts") or []:
        payload = pa.parse_cost(art.get("metadata")) if isinstance(art, dict) else None
        if payload:
            found = payload
    if found:
        return found
    msg = (task.get("status") or {}).get("message") or {}
    return pa.parse_cost(msg.get("metadata")) if isinstance(msg, dict) else None


_TERMINAL = {"completed", "failed", "canceled"}  # v0.3 spellings (back-compat)


def _is_input_required(state) -> bool:
    """True when the task parked on a human-input interrupt (1.0
    ``TASK_STATE_INPUT_REQUIRED`` / v0.3 ``input-required``) — not terminal, but
    polling it can never converge without a human."""
    return "INPUT" in str(state or "").upper().replace("-", "_")


def _is_terminal(state) -> bool:
    """True for A2A 1.0 terminal task states (``TASK_STATE_COMPLETED`` / ``FAILED``
    / ``CANCELLED`` / ``REJECTED``) and their v0.3 lowercase spellings."""
    return str(state or "").upper().endswith(("COMPLETED", "FAILED", "CANCELED", "CANCELLED", "REJECTED"))
