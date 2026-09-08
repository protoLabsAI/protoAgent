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

from dataclasses import dataclass

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
    parts = [p.get("text", "") for p in (task.get("parts") or []) if p.get("text")]
    text = "".join(parts)
    if text:
        return text
    msg = (task.get("status") or {}).get("message") or {}
    parts = [p.get("text", "") for p in (msg.get("parts") or []) if p.get("text")]
    text = "".join(parts)
    return text or None


def _extract_context_id(result) -> str:
    """The A2A ``contextId`` off a ``SendMessage`` / ``GetTask`` result, or ``""``.

    ``contextId`` is the protocol's grouping key for "these messages are one
    conversation" — the value a client echoes back to keep talking to the same peer
    context (#3360). The server owns it, so this only ever READS one; a peer that
    assigns none yields ``""`` and the caller sends none, which is the wire as it was.

    Tolerant of the same envelope variety as ``_extract_text``: the ``{"task": …}``
    ``SendMessage`` envelope, the bare task a ``GetTask`` answers with, and a peer that
    replies with a bare Message instead of a task (its id rides the message itself, or
    the terminal status message).

    A non-string ``contextId`` is treated as absent rather than coerced. The value is
    ECHOED onto the next request, and an a2a-sdk peer's ``ParseDict`` rejects a request
    whose ``contextId`` is not a string — so ``str()``-ing a number or an object here
    would turn one out-of-spec reply into a JSON-RPC error on every later address in that
    conversation. Reading nothing degrades; echoing junk breaks.
    """
    if not isinstance(result, dict):
        return ""
    task = _task_of(result)
    candidates = (task, result.get("message"), (task.get("status") or {}).get("message"))
    for envelope in candidates:
        if isinstance(envelope, dict) and isinstance(envelope.get("contextId"), str) and envelope["contextId"]:
            return envelope["contextId"]
    return ""


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
    / ``CANCELLED`` / ``REJECTED``) and their v0.3 lowercase spellings.

    A polling-STOP predicate ONLY — it answers "has the peer stopped working on this
    task", never "is this response's text the delegate's answer". Those are different
    questions (#3362): a FAILED task is terminal but its status message is a diagnostic,
    not an answer. Answer eligibility lives in :func:`classify_answer` below and MUST NOT
    be re-derived from this.
    """
    return str(state or "").upper().endswith(("COMPLETED", "FAILED", "CANCELED", "CANCELLED", "REJECTED"))


# ── answer eligibility (#3362) ────────────────────────────────────────────────
#
# ``_is_terminal`` stops the poll loop; it does NOT decide whether a result's text is the
# delegate's ANSWER. Conflating the two is the bug this section corrects — a WORKING
# task's status message, or a FAILED task's error message, is text the adapter must never
# hand back as the reply. Only a terminal COMPLETED task (1.0 ``TASK_STATE_COMPLETED`` /
# v0.3 ``completed``) yields an answer, plus a genuine bare Message reply — a peer that
# answers SendMessage with a Message and no task envelope, the pre-task compatibility
# shape. Everything else (WORKING/SUBMITTED, FAILED/CANCELED/REJECTED, INPUT_REQUIRED, or
# a task envelope whose state we cannot read) is not an answer.

#: The classes :func:`classify_answer` returns via ``AnswerClass.kind``.
ANSWER_COMPLETED = "completed"  # a terminal COMPLETED task — its text IS the answer
ANSWER_BARE_MESSAGE = "bare_message"  # no task envelope — a genuine Message reply (compat)
ANSWER_INPUT_REQUIRED = "input_required"  # parked on a human-input interrupt (the HITL park)
ANSWER_FAILED = "failed"  # terminal FAILED / CANCELED / REJECTED — a diagnostic, never an answer
ANSWER_PENDING = "pending"  # a task envelope not (yet) answerable — WORKING / unknown state


def _has_task_envelope(result) -> bool:
    """True when ``result`` carries an A2A task envelope — SendMessage's ``{"task": …}``
    or a bare ``GetTask`` task — as opposed to a genuine bare Message reply.

    The distinction gates answer eligibility (#3362): a bare Message's text is a valid
    answer (pre-task compatibility), but a task envelope whose state we cannot read is NOT
    — it is an in-flight or malformed task, and must never be mistaken for a bare Message
    just because it happens to carry text.

    Keys on the state-bearing markers ONLY — the ``{"task": …}`` wrapper, an explicit
    ``kind: task``, or a ``status`` object — deliberately NOT on ``artifacts`` alone: a
    status-less reply that carries artifacts (no task wrapper, no status) is the
    compatibility shape a pre-1.0 / minimal peer sends, and it degrades to the bare-Message
    answer path exactly as it did before this change."""
    if not isinstance(result, dict):
        return False
    if isinstance(result.get("task"), dict):
        return True
    if str(result.get("kind") or "").lower() == "task":
        return True
    return isinstance(result.get("status"), dict)


def _is_completed(state) -> bool:
    """True for A2A 1.0 ``TASK_STATE_COMPLETED`` and its v0.3 ``completed`` spelling —
    the ONLY terminal state whose text is eligible to be returned as an answer."""
    return str(state or "").upper().endswith("COMPLETED")


def _is_failed(state) -> bool:
    """True for the terminal FAILURE states — 1.0 ``TASK_STATE_FAILED`` / ``CANCELLED`` /
    ``REJECTED`` (and v0.3 ``canceled``). These are DIAGNOSTIC terminals, never answers."""
    return str(state or "").upper().endswith(("FAILED", "CANCELED", "CANCELLED", "REJECTED"))


def state_name(state) -> str:
    """A short, lowercase, legible name for a task state, for DIAGNOSTIC messages — not a
    wire value. ``TASK_STATE_FAILED`` → ``failed``; ``CANCELLED`` normalizes to
    ``canceled``; absent → ``unknown``; anything unrecognized is lowercased as-is."""
    s = str(state or "").upper().replace("-", "_")
    if not s:
        return "unknown"
    for name in ("COMPLETED", "FAILED", "CANCELLED", "CANCELED", "REJECTED", "INPUT_REQUIRED", "WORKING", "SUBMITTED"):
        if s.endswith(name):
            return "canceled" if name in ("CANCELLED", "CANCELED") else name.lower()
    return s.lower()


@dataclass(frozen=True)
class AnswerClass:
    """How :func:`classify_answer` reads an A2A result for ANSWER eligibility (#3362).

    ``kind`` is one of the ``ANSWER_*`` constants; ``state`` is the raw task-state string
    (``""`` for a bare Message). ``answerable`` is the single question the adapter asks
    before it may return ``_extract_text`` as the delegate's reply.
    """

    kind: str
    state: str

    @property
    def answerable(self) -> bool:
        """True only where this response's text may be RETURNED as the answer: a terminal
        COMPLETED task, or a genuine bare Message. Never WORKING / FAILED / INPUT_REQUIRED
        / a task envelope with no usable state."""
        return self.kind in (ANSWER_COMPLETED, ANSWER_BARE_MESSAGE)

    @property
    def completed(self) -> bool:
        """A terminal COMPLETED task — the only task class that can learn room continuity
        (#3360/#3362). Genuine bare Message answers may also carry a peer ``contextId``."""
        return self.kind == ANSWER_COMPLETED

    @property
    def failed(self) -> bool:
        """A terminal FAILED / CANCELED / REJECTED task — surface as a diagnostic error."""
        return self.kind == ANSWER_FAILED

    @property
    def input_required(self) -> bool:
        """Parked on a human-input interrupt — hand back the question + resume handle."""
        return self.kind == ANSWER_INPUT_REQUIRED


def classify_answer(result) -> AnswerClass:
    """Classify an A2A 1.0 SendMessage / GetTask ``result`` for answer eligibility (#3362).

    Deliberately kept apart from :func:`_is_terminal` (a polling-stop predicate): the
    adapter calls this to decide whether the observed result is the delegate's ANSWER, an
    error to raise, a park to hand back, or a task still worth polling.

    Order is load-bearing. An INPUT_REQUIRED park is recognized FIRST, before the
    bare-Message fallback, so a synchronous inline park is never mistaken for a Message. A
    task envelope with no usable state then falls to PENDING rather than masquerading as a
    bare Message — that is the whole point of ``_has_task_envelope`` sitting between the two.
    """
    state = (_task_of(result).get("status") or {}).get("state")
    raw = str(state or "")
    if _is_input_required(state):
        return AnswerClass(ANSWER_INPUT_REQUIRED, raw)
    if not _has_task_envelope(result):
        return AnswerClass(ANSWER_BARE_MESSAGE, raw)
    if _is_completed(state):
        return AnswerClass(ANSWER_COMPLETED, raw)
    if _is_failed(state):
        return AnswerClass(ANSWER_FAILED, raw)
    return AnswerClass(ANSWER_PENDING, raw)
