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

import asyncio
import json
import logging
import math
import os
import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger("protoagent.plugins.delegates")


class DelegateError(Exception):
    """A dispatch/parse failure. The caller turns it into a tool error string."""


# How much of a JSON-RPC error's free-form ``data`` member survives into the message the
# delegating agent reads. Generous enough for a peer's traceback, capped so a peer that
# echoes a whole request body can't flood the caller's context.
_A2A_ERROR_DETAIL_LIMIT = 2000


def _is_loopback_url(url: str) -> bool:
    """True when ``url`` targets this box's loopback interface — i.e. an in-instance
    member/board (its own workspace port, or the hub's own port for a ``host`` tenant
    path). Used to decide whether an otherwise-tokenless delegate may present the fleet
    service token (ADR 0089): loopback only — the token never rides off the box."""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "::1") or host == "127.0.0.1" or host.startswith("127.")


# ── field schema (drives the panel form + validation) ─────────────────────────


@dataclass
class FieldSpec:
    key: str  # dotted config key, e.g. "auth.token"
    label: str
    kind: str = "text"  # text | secret | args | path | number | textarea | select | envmap
    required: bool = False
    help: str = ""
    placeholder: str = ""
    options: list[str] = field(default_factory=list)  # for kind=select
    default: object = None
    # Tier: True ⇒ the console collapses this field behind "Advanced". Reserved for fields
    # with a sane default that most delegates never change (timeouts, git lifecycle, env).
    # It lives HERE rather than as a list of key names in the console because this schema is
    # already the single source the form is generated from — so a plugin or fork that adds a
    # field classifies it in the same place it declares it, instead of the console needing to
    # know about a field it has never heard of. Purely presentational: nothing about
    # parsing, validation, or defaults changes.
    advanced: bool = False

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "help": self.help,
            "placeholder": self.placeholder,
            "options": self.options,
            "default": self.default,
            "advanced": self.advanced,
        }


# ── the unified delegate model ────────────────────────────────────────────────


@dataclass
class Delegate:
    """One dispatch target, switched on ``type``."""

    name: str
    type: str
    description: str = ""

    # a2a
    url: str = ""
    auth_scheme: str = ""  # "" | bearer | apiKey
    auth_token: str = ""  # secret value (from secrets.yaml overlay)
    poll_timeout_s: float = 300.0  # a2a: max seconds to await a long-running delegated task

    # openai
    model: str = ""
    api_key: str = ""  # secret value
    system_prompt: str = ""
    max_tokens: int = 1024
    temperature: float = 0.4

    # acp
    command: str = ""
    args: list[str] = field(default_factory=list)
    workdir: str = ""
    env: dict[str, str] = field(default_factory=dict)
    # Subtractive env seam (#2117): host var names/prefixes to strip from the spawned
    # coder's inherited environment (trailing ``_`` ⇒ prefix-match, else exact-name),
    # applied BEFORE the additive ``env`` overlay in acp_client `_launch_env`. Lets a
    # caller sanitize host identity/credentials (``PROTOAGENT_*``, ``A2A_AUTH_TOKEN``)
    # WITHOUT mutating ``os.environ``. Names are not secrets — no redaction needed.
    env_remove: list[str] = field(default_factory=list)
    timeout_s: float = 600.0
    permissions: str = "auto"
    allow_kinds: list[str] = field(default_factory=list)
    deny_kinds: list[str] = field(default_factory=list)
    # Per-invocation ACP-only values. The registry sets these on an immutable
    # dataclass copy; they are never parsed from or persisted to delegate config.
    conversation_key: str = ""
    permissions_ceiling: str = ""
    confirm: bool = False
    # acp managed git (ADR 0076): the framework owns branch/commit/push/PR; the
    # coder edits files only. Off by default — non-worktree setups keep the old
    # coder-owns-git mode.
    manage_git: bool = False
    base_branch: str = "main"
    branch_prefix: str = ""  # empty ⇒ the delegate's name


def _secret(raw: dict, value_key: str, env_key: str) -> str:
    """Resolve a secret: explicit value (from the secrets.yaml overlay) wins;
    else read the named env var (``<field>_env``) if given. Never logs the value."""
    val = str(raw.get(value_key) or "").strip()
    if val:
        return val
    env_name = str(raw.get(env_key) or "").strip()
    return os.environ.get(env_name, "") if env_name else ""


# Substrings that mark a config key — or a per-delegate ``env`` var NAME — as
# secret-bearing. A matching env value is auto-routed to secrets.yaml on save and
# redacted on read even without an explicit per-row secret toggle. Shared by the
# API (redaction) and store (secret routing) so both agree on what counts.
_SECRETISH = ("key", "apikey", "token", "secret", "password", "passwd", "credential", "auth", "oauth", "bearer")

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def is_secretish(name: object) -> bool:
    """Token-boundary match — `AUTH_TOKEN` and `API_KEY` are secretish; substrings
    inside larger words are NOT (`GIT_AUTHOR_NAME` must never auto-route to
    secrets.yaml — QA panel on #2150). camelCase isn't split; env vars are
    conventionally SNAKE_CASE and a false negative just means the operator uses
    the explicit secret toggle."""
    parts = _TOKEN_SPLIT.split(str(name).lower())
    return any(p in _SECRETISH for p in parts)


# ── per-delegate environment (#2114) ──────────────────────────────────────────
#
# The env editor is available on EVERY adapter type: the built-in a2a/openai
# dispatchers don't consume ``env``/``env_remove`` (only acp's spawn does), but
# plugins and forks do, and authoring an env-carrying delegate from the console
# beats hand-editing YAML. A per-row **secret** toggle routes a value to
# secrets.yaml (see ``store._route_secret``) so API tokens never sit in plaintext
# config — the same posture as ``auth.token`` / ``api_key``.


def _env_fields() -> list[FieldSpec]:
    """The shared env editor fields appended to every adapter's schema. One
    ``envmap`` field drives the whole editor (key/value rows + a per-row secret
    toggle + the ``env_remove`` list) on the console form."""
    return [
        FieldSpec(
            "env",
            "Environment",
            "envmap",
            # Advanced on every type: the env editor is the single largest control on the
            # form (key/value rows + secret toggles + the removal list) and most delegates
            # never set one.
            advanced=True,
            help=(
                "Extra environment variables for the spawned delegate. Values are verbatim — no "
                "${VAR} expansion — and merge OVER the inherited process env AFTER the removals "
                "below strip it (remove-then-add). Toggle a row **secret** to store its value in "
                "secrets.yaml (gitignored), never in tracked config; on edit a secret row shows "
                "set-but-masked — leave it blank to keep the stored value."
            ),
        ),
    ]


def _parse_env(raw: dict, d: Delegate) -> None:
    """Parse ``env`` / ``env_remove`` off a raw config dict onto ``d``. Shared by
    every adapter so the console-authored env round-trips for all types (the
    ``Delegate`` dataclass already carries both fields)."""
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    d.env = {str(k): str(v) for k, v in env.items()}
    # Subtractive env seam (#2117): host var names/prefixes to strip from the spawned
    # coder before the additive ``env`` overlay applies (acp_client `_launch_env`).
    env_remove = raw.get("env_remove")
    d.env_remove = [str(x) for x in env_remove if str(x)] if isinstance(env_remove, (list, tuple)) else []


# ── adapters ──────────────────────────────────────────────────────────────────


class Adapter:
    """Base class. Subclasses set ``type`` and implement schema/parse/dispatch."""

    type: str = ""
    label: str = ""
    blurb: str = ""

    def config_schema(self) -> list[FieldSpec]:
        raise NotImplementedError

    def parse(self, raw: dict) -> Delegate:
        raise NotImplementedError

    async def dispatch(
        self,
        d: Delegate,
        query: str,
        *,
        timeout: float | None = None,
        item_id: str | None = None,
        resume_task_id: str | None = None,
    ) -> str:
        """Dispatch ``query`` to ``d``. ``item_id`` is the work-item identity used by
        adapters that manage a git lifecycle (acp, ADR 0076); ``resume_task_id``
        answers a PARKED a2a task (the HITL chain — see A2aAdapter). Adapters the
        concept doesn't apply to accept and ignore both, so the registry can
        forward them uniformly."""
        raise NotImplementedError

    async def probe(self, d: Delegate) -> dict:
        """Reachability check for the panel's Test button: {ok, latency_ms, error}."""
        return {"ok": None, "error": "probe not implemented for this type"}

    # secret field this type stores (for the CRUD secret overlay), as a dotted
    # path into the raw entry. None ⇒ no secret.
    secret_field: str | None = None

    # Shared helpers ---------------------------------------------------------
    @staticmethod
    def _base(raw: dict) -> dict:
        name = str(raw.get("name", "")).strip()
        if not name:
            raise DelegateError("delegate needs a name")
        return {
            "name": name,
            "type": str(raw.get("type", "")).strip(),
            "description": str(raw.get("description", "")).strip(),
        }


async def _timed(coro) -> tuple[object, int]:
    """Await ``coro``, returning (result, elapsed_ms)."""
    import time

    t0 = time.monotonic()
    res = await coro
    return res, int((time.monotonic() - t0) * 1000)


def _a2a_error_detail(d: Delegate, err: object) -> str:
    """Turn a JSON-RPC error payload into an operator-legible cause — especially the
    version-skew case, which otherwise surfaces as an opaque ``-32009``.

    Every other code keeps its ``code`` **and** its ``data`` member. ``-32603 "Internal
    error"`` is the generic JSON-RPC code: reading only ``message`` reduced a peer's real
    failure to the two least informative words it could have sent, with the cause sitting
    unread in ``data``."""
    code = err.get("code") if isinstance(err, dict) else None
    msg = str(err.get("message") or "").strip() if isinstance(err, dict) else str(err)
    if code == -32009 or "VERSION_NOT_SUPPORTED" in str(err).upper():
        return (
            f"delegate {d.name!r}: peer rejected A2A-Version 1.0 (VERSION_NOT_SUPPORTED) — it speaks "
            "an older A2A dialect. Upgrade the peer, or point its url at a 1.0 /a2a endpoint."
        )
    if not isinstance(err, dict):
        return f"delegate {d.name!r}: {err}"
    head = msg or "(no message)"
    if code is not None:
        head = f"{head} (JSON-RPC {code})"
    data = err.get("data")
    if data in (None, "", {}, []):
        return f"delegate {d.name!r}: {head}"
    detail = data if isinstance(data, str) else json.dumps(data, default=str)
    return f"delegate {d.name!r}: {head}: {' '.join(str(detail).split())[:_A2A_ERROR_DETAIL_LIMIT]}"


# The A2A protocol version(s) our delegate client can speak (it sends the
# ``A2A-Version: 1.0`` header + the 1.0 SendMessage/GetTask dialect). Used to
# pre-check a peer's advertised version and fail fast on a clear mismatch.
_A2A_SUPPORTED_VERSIONS = ("1.0",)


def _advertised_a2a_versions(card: dict) -> list[str]:
    """Every A2A protocol version a peer's agent-card advertises (de-duped, in
    first-seen order), or ``[]`` if the card says nothing about it.

    Reads the native proto field (``supportedInterfaces[].protocolVersion``) AND
    the proto-free top-level hint (``protocolVersion`` / ``supportedVersions``)
    that protoLabs agents also expose — so an older or non-protoLabs peer is still
    understood when it advertises its version in either shape. ``[]`` means
    *don't know* (older peers, partial cards): callers must treat that as
    best-effort and NOT block."""
    if not isinstance(card, dict):
        return []
    seen: list[str] = []

    def _add(v: object) -> None:
        s = str(v or "").strip()
        if s and s not in seen:
            seen.append(s)

    for iface in card.get("supportedInterfaces") or []:
        if isinstance(iface, dict):
            _add(iface.get("protocolVersion"))
    _add(card.get("protocolVersion"))
    versions = card.get("supportedVersions")
    if isinstance(versions, (list, tuple)):
        for v in versions:
            _add(v)
    return seen


# ── the peer's own cost-v1 telemetry (#3016) ──────────────────────────────────
#
# A protoAgent peer measures the turn it just ran for us and ships the numbers back on
# the wire (``tools/a2a_parse._extract_cost`` reads them off the terminal artifact).
# This is the one delegation whose spend we never have to estimate — the peer already
# computed it, with its own pricing, for the work we caused — so the adapter takes it
# verbatim and bills it to the calling turn instead of dropping it on the floor.


# Bounds on the numbers a peer reports about itself (#3038).
#
# This is the first path in the codebase where a REMOTE party's number reaches local
# storage, so magnitude is a trust boundary and not tidiness. A peer answering with
# ``{"usage": {"input_tokens": 1e308}}`` becomes a 309-digit Python int, which the
# executor accumulates, ``record_turn`` folds into ``total_tokens``, and SQLite then
# refuses: ``OverflowError: Python int too large to convert to SQLite INTEGER``.
# ``record_turn`` swallows that because telemetry is best-effort — correct in isolation,
# and it handed a peer a way to delete the CALLING turn's whole row, the lead agent's
# own genuine spend with it.
#
# SQLite's signed-64-bit INTEGER is the hard wall; these ceilings sit far below it,
# because a per-turn count that large is not a real turn and a bound you can reason
# about beats one that merely avoids the crash. Negatives floor at zero: no peer gets to
# issue a token credit, and a negative would drive ``record_turn``'s disjoint prompt
# split (#3003) somewhere no consumer expects.
_MAX_WIRE_TOKENS = 1_000_000_000_000  # 1e12 tokens in one turn is not a turn
_MAX_WIRE_COST_USD = 1_000_000.0  # …nor is a million dollars of it

#: Delegates already warned about in this process. See :func:`_note_clamped`.
_clamp_warned: set[str] = set()


def _note_clamped(delegate: str, fields: list[str]) -> None:
    """Say ONCE PER PEER that its reported numbers were out of range (#3038).

    The dropped row was never truly silent — ``record_turn`` logs the ``OverflowError``
    at ERROR with a traceback naming the task. What it cannot say is WHICH remote party
    caused it, because by the time SQLite refuses the row the peer's number is one
    addend inside ``total_tokens``. Attribution is what this line adds.

    Once per peer, not once per delegation: the clamp sits on the hot path of every
    delegation and a peer that reports garbage reports it on every reply, so an
    unguarded warning is a log flood a hostile party sets the volume of. The budget is
    keyed on the delegate name, which bounds it by the delegate REGISTRY — an operator's
    list, not a remote party's — and, unlike a single process-wide token, one peer's
    clamp cannot spend the warning the next peer's clamp needs.
    """
    detail = ", ".join(fields)
    if delegate in _clamp_warned:
        logger.debug("[delegates] clamped out-of-range cost-v1 from %r: %s", delegate, detail)
        return
    _clamp_warned.add(delegate)
    logger.warning(
        "[delegates] peer %r reported out-of-range cost-v1 values (%s) — clamped to sane bounds (#3038). "
        "Later clamps from this peer are logged at DEBUG.",
        delegate,
        detail,
    )


def _wire_number(
    value,
    ceiling: float = _MAX_WIRE_COST_USD,
    *,
    field: str = "",
    clamped: list | None = None,
    unknown: list | None = None,
) -> float:
    """A wire number as a finite float bounded to ``[0, ceiling]``.

    Proto-JSON round-trips numbers as floats and a foreign peer may send them as
    strings, so every field is coerced rather than trusted. The two ways a value can
    fail are kept apart, because they say different things about the peer:

    * **out of range** — a real magnitude that is absurd. Bounded to ``0`` or
      ``ceiling``, and ``field`` is appended to ``clamped`` so the caller — the one that
      knows WHICH peer sent it — can report it.
    * **non-finite** — ``Infinity`` (JSON parses it, and ``1e400`` overflows to it) or
      ``NaN``. That says the peer lost track, not that it spent a lot, so it bills
      nothing, which is #3016's settled contract; ``field`` goes to ``unknown``, never
      to ``clamped``. Rejecting it here is also what keeps ``int(inf)`` — an
      ``OverflowError``, not the ``ValueError`` a coercion guard expects — out of the
      caller, and an infinite ``cost_usd`` out of the turn's sums.
    """
    oversized = False
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    except OverflowError:
        # The natural wire spelling of an absurd number, and the one spelling the first
        # cut of this bound missed entirely (#3038): ``json.loads`` decodes a 400-digit
        # integer LITERAL to a Python int, and ``float()`` REFUSES an int wider than a
        # double rather than overflowing to inf the way the string ``"1e400"`` does.
        # Uncaught it raised out of :func:`_peer_usage_row` into ``_bill_peer_usage``'s
        # catch-all, which erased the peer's whole cost-v1 — the opposite of bounding
        # it. It is out of range, not unknown, so it takes the ceiling like any other
        # oversized magnitude.
        oversized = True
        n = -math.inf if isinstance(value, int) and value < 0 else math.inf
    if not oversized and not math.isfinite(n):
        if unknown is not None and field:
            unknown.append(field)
        return 0.0
    if n < 0:
        bounded = 0.0
    elif n > ceiling:
        bounded = ceiling
    else:
        return n
    if clamped is not None and field:
        clamped.append(field)
    return bounded


def _wire_int(
    value,
    ceiling: int = _MAX_WIRE_TOKENS,
    *,
    field: str = "",
    clamped: list | None = None,
    unknown: list | None = None,
) -> int:
    """A wire number as an int in ``[0, ceiling]`` — :func:`_wire_number` truncated
    toward zero. The default ceiling is exactly representable as a float, so the round
    trip through :func:`_wire_number` cannot land the clamp above the bound."""
    return int(_wire_number(value, float(ceiling), field=field, clamped=clamped, unknown=unknown))


def _peer_usage_row(result, delegate: str) -> dict | None:
    """A peer's cost-v1 payload as a turn-accumulator usage row (#3016), or ``None``.

    The shape is the one the A2A executor's ``usage`` lane already sums —
    ``{input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens, cost_usd, model}`` — plus a ``peer`` tag: the #2872
    precedent applied to a peer, so delegated spend bills to the PARENT turn while
    staying identifiable, and the tag keeps the peer's prompt size out of the lead
    thread's context-window fill (a peer's context is not ours).

    ``input_tokens`` is carried through cache-INCLUSIVE, the LangChain
    ``usage_metadata`` convention the peer accumulated it under and the one this side's
    accumulator and ``record_turn`` expect (#3003) — so the stored row's disjoint
    prompt split stays correct with a peer's tokens in it.

    ``cost_usd`` is the peer's OWN ``costUsd``, never re-derived here: the peer knows
    which models it actually routed to and we don't, so re-pricing would be a guess
    dressed as a measurement. A peer that reports tokens but no ``costUsd`` therefore
    contributes tokens and no cost — a visible undercount rather than an invented number.

    Every field is BOUNDED on the way in (#3038): a peer is a remote party, and an
    unbounded number of its choosing used to take the calling turn's whole telemetry
    row down with it. See :data:`_MAX_WIRE_TOKENS`.

    ``None`` when the peer emitted no cost-v1 (any non-protoAgent A2A agent) or when the
    payload carries neither tokens nor a cost: the caller then behaves exactly as before.
    """
    from tools.a2a_parse import PEER_MODEL_PREFIX, _extract_cost

    payload = _extract_cost(result)
    if not payload:
        return None
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    clamped: list[str] = []
    unknown: list[str] = []

    def _tokens(name: str) -> int:
        return _wire_int(usage.get(name), field=name, clamped=clamped, unknown=unknown)

    row = {
        "input_tokens": _tokens("input_tokens"),
        "output_tokens": _tokens("output_tokens"),
        "cache_read_input_tokens": _tokens("cache_read_input_tokens"),
        "cache_creation_input_tokens": _tokens("cache_creation_input_tokens"),
        "cost_usd": _wire_number(payload.get("costUsd"), field="costUsd", clamped=clamped, unknown=unknown),
    }
    if not any(row.values()):
        return None
    # Reported only once the row is one we will actually BILL, and only for values that
    # were really out of range (#3038). A payload that contributes nothing is dropped
    # above exactly as it was before, and spending the once-per-peer warning on a row no
    # consumer ever sees would leave a later, real clamp with nothing left to say it
    # with. Non-finite is the other half: an ordinary upstream condition with a settled
    # contract (#3016 — bill nothing, the peer lost track), so it goes to debug and
    # never names the peer in a line that claims its numbers were clamped to a bound.
    if clamped:
        _note_clamped(delegate, clamped)
    if unknown:
        logger.debug("[delegates] non-finite cost-v1 from %r billed as zero: %s", delegate, ", ".join(unknown))
    # A marker, not a model name: cost-v1 carries no model, and inventing one would break
    # the promise that `models` proves which model actually ran (ADR 0006 Slice 4b). The
    # prefix is what makes "what did this turn spend on peers" answerable from the stored
    # row without a second telemetry row per delegation — it is the only durable trace a
    # peer leaves, since the `peer` tag below is a stream-only routing hint. Every reader
    # that picks the model that RAN filters markers out with `a2a_parse.drop_peer_markers`.
    row["model"] = f"{PEER_MODEL_PREFIX}{delegate}"
    row["peer"] = delegate
    return row


# A detached background delegation must bill NOTHING, and saying so takes a flag (#3016).
#
# ``asyncio.create_task`` COPIES the spawning context, so the job that
# ``_spawn_background_delegation`` fires from inside the ``delegate_to`` tool body inherits
# that body's LangChain run context — the callback manager ``adispatch_custom_event`` reads.
# Left alone, a detached delegation therefore bills the spawning turn *if the peer answers
# while that turn is still streaming* and is silently dropped if it answers after: the same
# delegation producing two different telemetry rows depending on how fast the peer was.
# A detached job is not the spawning turn's work — it is delivered to a LATER turn — so it
# is excluded deterministically instead, which is also what ADR 0006 documents.
_DETACHED_DELEGATION: ContextVar[bool] = ContextVar("protoagent_detached_delegation", default=False)


def mark_delegation_detached() -> None:
    """Mark this coroutine's context as a detached background delegation (ADR 0050).

    Called at the top of the background job's own coroutine, so the flag is set in the
    context ``asyncio.create_task`` copied for it and never leaks back to the spawning
    turn (a contextvar set inside a task is that task's alone)."""
    _DETACHED_DELEGATION.set(True)


async def _bill_peer_usage(result, delegate: str) -> None:
    """Surface a peer's reported spend onto the CALLING turn's stream (#3016).

    Dispatched as a custom ``usage`` event from inside the ``delegate_to`` tool body's
    call stack — the lane #2872 built for subagents: ``server/chat.py`` forwards each one
    verbatim as a ``("usage", …)`` frame and the A2A executor's accumulator sums it into
    the turn's tokens, cost, and models. Reusing that lane is what lets
    ``Adapter.dispatch`` keep its ``-> str`` contract (stable across three adapters), and
    is why nothing is stashed on the adapter: ``ADAPTERS`` holds one instance per type for
    the whole process, so last-call state on it would race across a concurrent fan-out.

    Best-effort **end to end** — reading the payload included. A peer with no cost-v1
    emits nothing, having no run context at all (a unit test, the CLI runner) is not an
    error, and neither is a payload this cannot parse: telemetry must never turn a
    delegation that succeeded into one that failed. That is not hypothetical here — a
    raise would propagate through ``registry.dispatch``, which records the delegate as
    failing before re-raising, so a malformed number would discard an answer already in
    hand *and* put a red mark on a healthy peer.
    """
    if _DETACHED_DELEGATION.get():
        return
    try:
        row = _peer_usage_row(result, delegate)
        if not row:
            return
        from langchain_core.callbacks import adispatch_custom_event

        await adispatch_custom_event("usage", dict(row))
    except Exception:  # noqa: BLE001 — telemetry must never break a delegation
        logger.debug("[delegates] peer usage row not billed to the turn stream", exc_info=True)


class A2aAdapter(Adapter):
    type = "a2a"
    label = "A2A agent"
    blurb = "A fleet peer over the A2A JSON-RPC protocol."
    secret_field = "auth.token"

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "url",
                "URL",
                "text",
                required=True,
                placeholder="https://peer.example/a2a",
                help="The peer's A2A endpoint (usually ends in /a2a).",
            ),
            FieldSpec(
                "auth.scheme",
                "Auth scheme",
                "select",
                options=["", "bearer", "apiKey"],
                help="How the peer expects credentials, if any.",
            ),
            FieldSpec(
                "auth.token",
                "Auth token",
                "secret",
                help="Stored in secrets.yaml (gitignored), never in tracked config. If the "
                "peer has an auth.federation_token configured (ADR 0066), use THAT value "
                "here instead of their operator token — it reaches only /a2a + /v1 on "
                "their side, not their full operator surface.",
            ),
            FieldSpec(
                "poll_timeout_s",
                "Task poll timeout (s)",
                "number",
                default=300,
                advanced=True,
                help="Max seconds to wait for a long-running delegated task to finish before "
                "giving up locally — the peer keeps working. Also caps the initial synchronous "
                "SendMessage read, so a peer that answers inline (protoAgent's own server does) "
                "may legitimately take up to this long to reply — the old flat 60s hard-failed "
                "every member turn beyond it (#1778). Raise it for slow agents (e.g. a code build).",
            ),
            *_env_fields(),
        ]

    def parse(self, raw: dict) -> Delegate:
        d = Delegate(**self._base(raw))
        d.url = str(raw.get("url", "")).strip()
        if not d.url:
            raise DelegateError(f"a2a delegate {d.name!r} needs a url")
        auth = raw.get("auth") or {}
        d.auth_scheme = str(auth.get("scheme", "")).strip()
        d.auth_token = _secret(auth, "token", "credentialsEnv")
        try:
            d.poll_timeout_s = float(raw.get("poll_timeout_s") or 300.0)
        except (TypeError, ValueError):
            d.poll_timeout_s = 300.0
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
        import httpx  # noqa: F401 — used by the pre-flight probe below

        from observability import tracing
        from security import policy

        blocked = policy.check_url(d.url)
        if blocked:
            raise DelegateError(blocked.replace("destination", f"delegate {d.name!r}", 1))
        # Pre-flight protocol-version check (best-effort). Fetch the peer's card and, if
        # it CLEARLY advertises an A2A protocol version we can't speak, fail fast with a
        # legible mismatch instead of sending and waiting for the opaque -32009
        # VERSION_NOT_SUPPORTED mid-dispatch. A silent/unreachable card never blocks — we
        # fall through to dispatch, whose -32009 mapping (``_a2a_error_detail``) still applies.
        info = await self.probe(d)
        advertised = info.get("supported_versions") or []
        if advertised and not any(v in _A2A_SUPPORTED_VERSIONS for v in advertised):
            raise DelegateError(
                f"delegate {d.name!r} advertises A2A protocol {'/'.join(advertised)} but this agent "
                f"speaks {'/'.join(_A2A_SUPPORTED_VERSIONS)} — refusing to dispatch (an older peer "
                "would reject the call with -32009 VERSION_NOT_SUPPORTED). Upgrade the peer, or point "
                "its url at a 1.0 /a2a endpoint."
            )
        # A2A-Version is mandatory for an a2a-sdk >=1.0 peer: a missing header defaults
        # to 0.3 on the receiver → -32009 VERSION_NOT_SUPPORTED (ADR 0051 audit). The
        # scheduler/inbox/background self-POSTs already set it; the delegate client must too.
        headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if d.auth_token:
            headers["Authorization"] = f"Bearer {d.auth_token}" if d.auth_scheme != "apiKey" else d.auth_token
            if d.auth_scheme == "apiKey":
                headers["X-API-Key"] = d.auth_token
        elif _is_loopback_url(d.url):
            # ADR 0089 D4: an in-instance delegate to a loopback member/board carries no
            # explicit credential (the "local board is tokenless" pattern, supervisor.py). Now
            # that members require a credential (D5), present the fleet service token so the
            # call still authenticates — the member accepts it as operator. Loopback ONLY: an
            # off-box delegate must configure its own token; the fleet token never leaves the box.
            try:
                from graph.fleet.service_token import resolve_service_token

                headers["Authorization"] = f"Bearer {resolve_service_token()}"
            except Exception:  # noqa: BLE001 — not in a fleet / no token: dispatch unauthenticated as before
                logger.debug("[delegates] no fleet service token for loopback delegate %r", d.name)

        async def _rpc(client, method, params):
            body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
            # Map transport failures to a legible CAUSE — a delegating agent (and the
            # operator) needs "unreachable" vs "timed out" vs "version-incompatible",
            # not an opaque stack trace or a bare connection error.
            try:
                r = await client.post(d.url, json=body, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise DelegateError(f"delegate {d.name!r} unreachable at {d.url} ({type(exc).__name__})") from exc
            except httpx.TimeoutException as exc:
                raise DelegateError(f"delegate {d.name!r} timed out contacting {d.url}") from exc
            except httpx.HTTPError as exc:
                raise DelegateError(f"delegate {d.name!r} transport error: {str(exc)[:160]}") from exc
            if r.status_code >= 400:
                raise DelegateError(f"delegate {d.name!r} HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if data.get("error"):
                raise DelegateError(_a2a_error_detail(d, data["error"]))
            return data.get("result") or {}

        poll_timeout = d.poll_timeout_s if d.poll_timeout_s and d.poll_timeout_s > 0 else 300.0

        # Boundary span for the OUTBOUND hop. Without it, a delegation is invisible on
        # the caller's side except as the enclosing `tool:delegate_to` observation, so
        # you cannot separate "the peer was slow" from "we were slow calling it" — the
        # first question anyone asks of a multi-agent trace. This span's duration IS the
        # cross-agent latency: dispatch → peer terminal, including the GetTask poll.
        #
        # Opened BEFORE the trace context is read below, deliberately: the peer then
        # nests under *this* span rather than the enclosing turn, so a delegation chain
        # reads as a real call tree instead of a flat list of sibling agents.
        with tracing.trace_span(
            f"a2a:{d.name}",
            metadata={"url": d.url, "delegate": d.name, "poll_timeout_s": poll_timeout},
            as_type="agent",
        ):
            return await self._dispatch_traced(
                d, query, send_timeout=timeout, poll_timeout=poll_timeout, _rpc=_rpc, resume_task_id=resume_task_id
            )

    async def _dispatch_traced(self, d, query, *, send_timeout, poll_timeout, _rpc, resume_task_id=None) -> str:
        """The wire half of ``dispatch``, inside the outbound span (see caller)."""
        import time

        import httpx

        from tools.a2a_parse import _extract_text, _is_input_required, _is_terminal

        # Mark protoAgent-originated peer delegation independently of tracing. The
        # receiver uses this only as operator-visible provenance; authorization is
        # still decided by the authenticated transport before the executor runs.
        # Stamp both levels because older SDK peers may preserve only one.
        provenance = {"origin": "a2a", "trigger": "delegate_to"}

        # Fleet tracing: when this dispatch runs inside a traced turn, hand the
        # peer our Langfuse trace context as ``a2a.trace`` metadata — the shape
        # a2a_impl/executor._extract_caller_trace reads on the receiving side
        # (camelCase traceId/spanId, threaded into trace_session as
        # caller_trace_id/caller_span_id → the peer JOINS our trace). Attached at
        # BOTH request level (preferred by the receiver's metadata merge) and
        # message level (fallback for peers whose SDK drops request metadata).
        send_params: dict = {
            "metadata": dict(provenance),
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": query}],
                "messageId": str(uuid.uuid4()),
                "metadata": dict(provenance),
            },
        }
        try:
            from observability import tracing

            tctx = tracing.current_trace_context()
        except Exception:  # noqa: BLE001 — tracing must never break a dispatch
            tctx = None
        if tctx:
            wire = {"traceId": tctx["trace_id"]}
            if tctx.get("span_id"):
                wire["spanId"] = tctx["span_id"]
            send_params["metadata"]["a2a.trace"] = wire
            send_params["message"]["metadata"]["a2a.trace"] = wire
        # A2A 1.0 (a2a-sdk >=1.0): JSON-RPC `SendMessage` / `GetTask`, the ROLE_USER
        # enum, and a `result.task` envelope. (`message/send` + lowercase `user` is
        # the v0.3 legacy dialect, which 1.0 servers reject with -32601.)
        #
        # Resume of a PARKED task (the HITL delegation chain): the caller answers a
        # question the peer raised mid-task. Wire shape proven by the handler tests:
        # SendMessage carrying the parked task's taskId + contextId re-enters
        # execute() with resume=True. We GetTask first for the contextId and to be
        # honest about state (already-terminal → return its text; gone → say so).
        if resume_task_id:
            send_params["message"]["taskId"] = resume_task_id

        # A *synchronous* peer — protoAgent's own A2A server answers SendMessage INLINE,
        # holding the connection open for the whole delegated turn before returning the final
        # Message — so the initial SendMessage READ must be allowed to run as long as the task
        # legitimately might (poll_timeout), NOT the flat 60s that hard-failed every member turn
        # >60s (#1778: hub→member delegation silently fell back on any non-trivial turn). Connect
        # stays short so an unreachable peer still fails fast; the same client serves the GetTask
        # poll loop, which returns quickly regardless. An explicit ``timeout`` still overrides.
        read_budget = send_timeout if send_timeout is not None else poll_timeout
        async with httpx.AsyncClient(timeout=httpx.Timeout(read_budget, connect=10.0)) as client:
            if resume_task_id:
                parked = await _rpc(client, "GetTask", {"id": resume_task_id})
                ptask = parked.get("task", parked) or {}
                pstate = (ptask.get("status") or {}).get("state")
                if _is_terminal(pstate):
                    done_text = _extract_text(parked)
                    return (
                        f"(task {resume_task_id} had already finished — state {pstate}; nothing to resume)"
                        + (f"\n\n{done_text}" if done_text else "")
                    )
                if not _is_input_required(pstate):
                    raise DelegateError(
                        f"delegate {d.name!r}: task {resume_task_id} is {pstate or 'unknown'}, not parked "
                        "for input — it can't be resumed with an answer."
                    )
                if ptask.get("contextId"):
                    send_params["message"]["contextId"] = ptask["contextId"]
            result = await _rpc(client, "SendMessage", send_params)
            task = result.get("task", result) or {}
            task_id = task.get("id")
            state = (task.get("status") or {}).get("state")
            # A park can come back INLINE: a synchronous peer answers SendMessage with
            # the task already in INPUT_REQUIRED, its question in status.message. The
            # text early-return must not swallow that — _extract_text would hand back
            # the bare question as if it were the ANSWER, losing the task id and the
            # resume protocol with it (caught live, 2026-08-20).
            text = _extract_text(result)
            if text and not _is_input_required(state):
                # Bill the peer's own cost-v1 telemetry to this turn before returning
                # the answer (#3016) — the synchronous path a protoAgent peer takes.
                await _bill_peer_usage(result, d.name)
                return text
            deadline = time.monotonic() + poll_timeout
            while task_id and not _is_terminal(state) and not _is_input_required(state) and time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                # A2A 1.0 GetTaskRequest is {tenant, id, history_length} — `id`, not
                # the v0.3 legacy `name` (proto: a2a.types.a2a_pb2.GetTaskRequest).
                # The old {"name": …} shape only ever worked against 0.3 peers; a 1.0
                # peer rejects/ignores it, so the poll loop could never converge for
                # an async-style peer (latent: protoAgent peers answer SendMessage
                # inline, so this path rarely ran).
                result = await _rpc(client, "GetTask", {"id": task_id})
                task = result.get("task", result) or {}
                state = (task.get("status") or {}).get("state")
            if _is_input_required(state):
                # The peer parked on an input interrupt. The HITL delegation chain
                # (operator decision, 2026-08-20): the QUESTION comes back to the
                # CALLING agent as a normal tool result with a resume handle — the
                # caller answers it (delegate_to(..., resume_task_id=…)); a caller
                # that can't answer escalates the same way to ITS caller (its own
                # ask_human parks ITS task, which bubbles another hop), so a chain
                # x→y→z ends at whichever console has a human. Never poll a park to
                # the deadline, and never bury the question in an error.
                question = _extract_text(result) or ""
                if not task_id:
                    raise DelegateError(
                        f"delegate {d.name!r} asked for input but returned no task id to resume"
                        + (f": {str(question)[:300]}" if question else "")
                    )
                return (
                    f"⏸ delegate {d.name!r} needs input before it can continue.\n"
                    + (f"Question: {str(question)[:1000]}\n" if question else "")
                    + f"Parked task: {task_id}\n\n"
                    f"To continue: answer with delegate_to(target={d.name!r}, "
                    f"query='<your answer>', resume_task_id={str(task_id)!r}). "
                    "If you can't answer it yourself, get the answer first — ask your own "
                    "operator (ask_human) if you have one; your question bubbles up the same "
                    "way — then resume with it."
                )
            text = _extract_text(result)
            if text:
                # Same billing as the inline path (#3016), for the peer that made us
                # poll. Deliberately NOT done on the two branches above. A park emits no
                # terminal artifact — only a status message — so its leg's spend is not
                # on the wire at all; the peer keeps its own row for it (#2943) and this
                # side undercounts a HITL chain by that much, documented in ADR 0006. And
                # the "already finished, nothing to resume" branch reports work an EARLIER
                # dispatch caused — billing it here would double-count it into a second
                # turn.
                await _bill_peer_usage(result, d.name)
                return text
            if task_id and not _is_terminal(state):
                raise DelegateError(
                    f"delegate {d.name!r} still running after {int(poll_timeout)}s — the peer may "
                    f"still be working; raise its poll timeout if tasks legitimately take longer "
                    f"(state={state})"
                )
            raise DelegateError(f"delegate {d.name!r} returned no text (state={state})")

    async def probe(self, d: Delegate) -> dict:
        import httpx

        from security import policy

        origin = d.url.split("/a2a")[0].rstrip("/") if "/a2a" in d.url else d.url.rstrip("/")
        card = f"{origin}/.well-known/agent-card.json"
        blocked = policy.check_url(card)
        if blocked:
            return {"ok": False, "error": blocked}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r, ms = await _timed(client.get(card))
            if r.status_code >= 400:
                return {"ok": False, "latency_ms": ms, "error": f"HTTP {r.status_code}"}
            body = r.json() or {}
            name = body.get("name", "")
            # Capture the peer's advertised A2A protocol version(s) so a caller (and
            # dispatch's pre-check) can fail fast on a version mismatch. ``version`` is
            # the peer's APP version (distinct); ``protocol_version`` is the primary
            # advertised protocol, "" when the card is silent (older peers).
            advertised = _advertised_a2a_versions(body)
            pv = advertised[0] if advertised else ""
            detail = f"agent-card OK ({name})" + (f", A2A {pv}" if pv else "")
            return {
                "ok": True,
                "latency_ms": ms,
                "protocol_version": pv,
                "supported_versions": advertised,
                "version": body.get("version", ""),
                "detail": detail,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}


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
                raise DelegateError(f"delegate {d.name!r} unreachable at {url} ({type(exc).__name__})") from exc
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


#: ACP ``stopReason`` values that mean the reply is CUT OFF rather than finished, mapped
#: to what the caller should do about it. ``prompt()`` returns text either way, so without
#: this an interrupted reply is indistinguishable from a complete one — the orchestrator
#: acts on a half-answer and the operator only finds out downstream (#2352).
#:
#: Its sibling is ``acp_client._UNFINISHED_STOP_REASONS``, which decides whether the run
#: RECORDS as a success. A reason marked incomplete here must be a failure there, or the
#: telemetry rollup books a success for a reply this module is telling the model not to
#: trust — pinned by a test, since the two lists live a package apart (#3015).
_INCOMPLETE_STOP_REASONS = {
    "max_tokens": (
        "the coding agent hit its output-token limit mid-generation, so this reply is CUT OFF "
        "and its final section may be missing or half-written. Do not treat it as complete: "
        "either re-dispatch the remaining work as a narrower follow-up query, or ask the "
        "delegate to continue from where it stopped."
    ),
    "refusal": (
        "the coding agent declined to finish this request, so the reply is incomplete. "
        "Re-dispatching the same query verbatim will decline again — restate the task or "
        "pick a different delegate."
    ),
}


def _mark_incomplete(reply: str, stop_reason: str | None) -> str:
    """Append an operator/model-visible marker when the coder's turn ended for a reason
    that means the text is truncated rather than finished.

    ``AcpClient`` records ``last_stop_reason`` on every turn and had no production reader
    at all — the signal existed and was dropped on the floor, which is exactly how a
    ``max_tokens`` cut-off reached the orchestrator looking like a finished answer. Kept
    OUT of ``prompt()`` itself: the coder ladder consumes replies as data and reads the
    stop reason directly via ``dead_end()``; it's the *delegate* surface, whose reply goes
    into a model's context as prose, that needs it spelled out.

    ``cancelled`` is deliberately absent — an operator stopping a turn already knows.

    Read immediately after the caller's own ``await client.prompt(...)`` returns. That is
    exactly as reliable as the reply text itself: both are per-client state the same call
    just wrote, on a client the pool already treats as single-flight (``prompt`` clears
    ``_answer`` on entry, so a concurrent same-delegate prompt would corrupt the reply
    long before it could confuse the stop reason).
    """
    reason = (stop_reason or "").strip()
    note = _INCOMPLETE_STOP_REASONS.get(reason)
    if not note:
        return reply
    return f"{reply}\n\n[incomplete reply — {note}]" if reply.strip() else f"[no reply — {note}]"


class AcpAdapter(Adapter):
    type = "acp"
    label = "Coding agent (ACP)"
    blurb = "A CLI coding agent (protoCLI, Claude Code, …) driven over ACP."

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "command",
                "Command",
                "text",
                required=True,
                placeholder="proto",
                help="Binary on PATH that speaks ACP (e.g. proto). For Claude Code use `claude-code` — an alias for the claude-agent-acp adapter.",
            ),
            FieldSpec(
                "args",
                "Args",
                "args",
                placeholder="--acp",
                help="Launch args (e.g. --acp). Leave empty for claude-code.",
            ),
            FieldSpec(
                "workdir",
                "Workdir",
                "path",
                required=True,
                placeholder="~/dev/my-repo",
                help="Session cwd — the confinement boundary.",
            ),
            FieldSpec(
                "permissions",
                "Permissions",
                "select",
                options=["auto", "allowlist", "readonly"],
                default="auto",
                help="By-kind permission policy for the agent's actions.",
            ),
            FieldSpec(
                "confirm",
                "Confirm each call",
                "select",
                options=["false", "true"],
                default="false",
                advanced=True,
                help="Ask the operator before each call.",
            ),
            FieldSpec("timeout_s", "Timeout (s)", "number", default=600, advanced=True),
            FieldSpec(
                "manage_git",
                "Managed git",
                "select",
                options=["false", "true"],
                default="false",
                advanced=True,
                help="Framework owns branch/commit/push/PR (ADR 0076); the coder only edits "
                "files. Needs workdir to be a git checkout with an `origin` remote.",
            ),
            FieldSpec(
                "base_branch",
                "Base branch",
                "text",
                placeholder="main",
                advanced=True,
                help="Managed git: branches are cut from fresh origin/<base> and PRs target it.",
            ),
            FieldSpec(
                "branch_prefix",
                "Branch prefix",
                "text",
                placeholder="(delegate name)",
                advanced=True,
                help="Managed git: branch names are <prefix>/<slug>-<id7>. Empty ⇒ the delegate's name.",
            ),
            *_env_fields(),
        ]

    def parse(self, raw: dict) -> Delegate:
        d = Delegate(**self._base(raw))
        d.command = str(raw.get("command", "")).strip()
        d.workdir = str(raw.get("workdir", "")).strip()
        if not (d.command and d.workdir):
            raise DelegateError(f"acp delegate {d.name!r} needs command + workdir")
        args = raw.get("args") or []
        d.args = [str(a) for a in args] if isinstance(args, (list, tuple)) else []
        # Convenience alias (issue #1116): Claude Code has NO native ACP mode — it needs
        # the claude-agent-acp adapter (formerly @zed-industries/claude-code-acp, now
        # deprecated). Let operators write the intuitive `command: claude-code` and map
        # it to the adapter binary, which takes no launch args — instead of hand-authoring
        # the incantation and getting an opaque `agent exited` at first dispatch.
        if d.command in ("claude-code", "claude-acp"):
            d.command = "claude-agent-acp"
            d.args = []
        _parse_env(raw, d)
        try:
            d.timeout_s = float(raw.get("timeout_s") or 600)
        except (TypeError, ValueError):
            d.timeout_s = 600.0
        d.permissions = str(raw.get("permissions", "auto")).strip().lower() or "auto"
        d.allow_kinds = [str(k).lower() for k in (raw.get("allow_kinds") or [])]
        d.deny_kinds = [str(k).lower() for k in (raw.get("deny_kinds") or [])]
        d.confirm = str(raw.get("confirm", "")).strip().lower() in ("1", "true", "yes")
        d.manage_git = str(raw.get("manage_git", "")).strip().lower() in ("1", "true", "yes")
        d.base_branch = str(raw.get("base_branch") or "main").strip() or "main"
        d.branch_prefix = str(raw.get("branch_prefix", "")).strip()
        return d

    @staticmethod
    def _spec(d: Delegate) -> dict:
        """The coding_agent spec dict for this delegate. Shared by ``dispatch`` and
        ``teardown`` so both compute the SAME client cache key (which includes
        ``workdir``) — so a caller that scopes ``workdir`` per call (e.g. via
        ``dataclasses.replace`` onto a disposable worktree) tears down the exact
        client it dispatched."""
        return {
            "name": d.name,
            "command": d.command,
            "args": d.args,
            "workdir": d.workdir,
            "env": d.env or None,
            "env_remove": d.env_remove,
            "permissions": d.permissions,
            "allow_kinds": d.allow_kinds,
            "deny_kinds": d.deny_kinds,
            "conversation_key": d.conversation_key,
            "permissions_ceiling": d.permissions_ceiling,
        }

    async def dispatch(
        self,
        d: Delegate,
        query: str,
        *,
        timeout: float | None = None,
        item_id: str | None = None,
        resume_task_id: str | None = None,
    ) -> str:
        if d.manage_git:
            return await self._dispatch_managed(d, query, timeout=timeout, item_id=item_id)
        return await self._prompt(d, query, timeout=timeout)

    async def _prompt(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        # Reuse the ADR 0024 ACP client + by-kind permission policy.
        from plugins.coding_agent import _client_for, _drop_client, _make_permission
        from plugins.coding_agent.acp_client import AcpError

        spec = self._spec(d)
        client = _client_for(spec)
        client._permission = _make_permission(spec)
        try:
            reply = await client.prompt(query, timeout=timeout or d.timeout_s)
            # getattr: the pool hands back whatever client the spec resolves to, and a
            # missing stop reason must degrade to 'no marker', never to an AttributeError
            # that turns a working delegate into a hard dispatch failure.
            return _mark_incomplete(reply, getattr(client, "last_stop_reason", None))
        except asyncio.CancelledError:
            # The turn was stopped (operator hit stop, or an orchestrator watchdog
            # fired). The client is POOLED, so without this its subprocess keeps
            # running detached — exactly "I stopped the main thread and the delegate
            # didn't stop". Drop it from the pool + SIGKILL the agent tree NOW
            # (synchronous, no awaits — we're mid-cancellation) before re-raising.
            _drop_client(spec)
            client.kill_now()
            raise
        except AcpError as exc:
            # Attribute it. `delegate_to` renders a DelegateError as a bare `Error: <msg>`,
            # so without the name a fan-out across several coders can't tell which one blew up.
            raise DelegateError(f"delegate {d.name!r} ({d.command}): {exc}") from exc

    async def _dispatch_managed(
        self, d: Delegate, query: str, *, timeout: float | None = None, item_id: str | None = None
    ) -> str:
        """Managed-git dispatch (ADR 0076): claim → PR pre-flight → branch setup →
        edit-only coder run → framework-owned commit/rebase/push/PR. The claim dedups
        in-flight duplicates (fan-out of one item); the PR pre-flight dedups across
        restarts. On a coder failure the worktree keeps its edits and a re-run's
        idempotent lifecycle adopts them."""
        from plugins.coding_agent import git_harness as harness

        iid = (item_id or "").strip() or harness.derive_item_id(query)
        # Name the work, not the wrapper: infer a conventional-commit title from the task
        # (project_board wraps features in a coder preamble whose first line is boilerplate,
        # which the deterministic first-line slug would otherwise bake into the branch/PR).
        # Best-effort — falls back to the deterministic title. Uniqueness is the id suffix.
        title = (await harness.infer_title(query)) or harness.title_from(query)
        branch = harness.mint_branch(d.branch_prefix or d.name, title, iid)
        holder = harness.claim(iid, d.name)
        if holder is not None:
            return (
                f"Item `{iid}` is already being built by {holder!r} (branch `{branch}`) — not "
                "dispatching a duplicate. Wait for the in-flight run instead of re-fanning this item."
            )
        try:
            workdir = os.path.expanduser(d.workdir)
            existing = await harness.preflight_pr(workdir, iid)
            if existing:
                return f"An open PR already exists for item `{iid}` (branch `{branch}`): {existing} — not dispatching a duplicate."
            prep = await harness.prepare(workdir, base=d.base_branch, branch=branch)
            if prep.error:
                return f"Error: managed-git setup for {d.name!r} failed: {prep.error}"
            reply = await self._prompt(d, query + harness.edit_only_directive(branch), timeout=timeout)
            outcome = await harness.finish(workdir, base=d.base_branch, branch=branch, item_id=iid, title=title)
            notes = "".join(f"\n- note: {n}" for n in prep.notes)
            return f"{reply}\n\n{outcome.render()}{notes}"
        finally:
            harness.release(iid)

    async def teardown(self, d: Delegate) -> bool:
        """Evict + terminate the cached ACP subprocess for this delegate.

        ``dispatch`` caches a long-lived ``AcpClient`` (subprocess + session) keyed
        partly on ``workdir``. A caller that dispatches into a transient, per-call
        ``workdir`` should call this when done (e.g. in a ``finally``) so the child
        is reaped rather than left running — a plain cache ``pop`` forgets the
        handle but leaves the process alive. Returns True if a live client was
        closed; no-op (False) if none was started. Idempotent."""
        from plugins.coding_agent import evict_clients

        return await evict_clients(self._spec(d))

    async def forget_session(self, d: Delegate) -> bool:
        """Forget this delegate's persisted ACP session so the next ``dispatch``
        starts a fresh ``session/new`` (vs reattaching the prior thread). For a
        caller that recreates ``workdir`` fresh per call (a disposable worktree), a
        resumed session's memory would reference a diff the wiped tree no longer has.
        See ``coding_agent.forget_session``. Idempotent."""
        from plugins.coding_agent import forget_session

        return await forget_session(self._spec(d))

    async def probe(self, d: Delegate) -> dict:
        """Reachability check for the panel's Test button (also the periodic health
        prober's per-delegate check).

        Does a REAL ACP `initialize` handshake (spawn the command, complete the
        protocol handshake, close) — and NOTHING more: no `session/new`, no
        `session/load`, no `session/prompt`. So a launch command that's on PATH and
        workdir-valid but doesn't actually speak ACP FAILS the probe instead of
        showing green (issue #1116 — the old PATH+workdir check passed `command:
        claude` while every dispatch failed with an opaque `agent exited`), while a
        probe stays genuinely cheap + side-effect-free: it never opens a session every
        120s the way the old `_ensure_started` path did (#1300).
        """
        import asyncio
        import os
        import shutil

        # Bare `claude` is on PATH but has no native ACP mode — the classic false-green.
        # Steer to the adapter instead of spawning it only to watch the handshake hang.
        if os.path.basename(d.command) == "claude":
            return {
                "ok": False,
                "error": (
                    "`claude` has no native ACP mode. Claude Code needs the claude-agent-acp "
                    "adapter — `npm i -g @agentclientprotocol/claude-agent-acp`, then set "
                    "command: claude-code (an alias) or claude-agent-acp."
                ),
            }
        # Resolve the command against the SAME PATH the real spawn will use — the
        # delegate's env PATH overlaid on the process PATH — not just os.environ.
        # The actual ACP launch merges d.env (acp_client `_launch_env`/`env=…`), so a
        # delegate that supplies its own PATH (or runs under the desktop app's
        # augmented PATH) would spawn fine, yet the probe's bare `shutil.which` still
        # red-X'd it. Probe and spawn now agree on where to look (#1299).
        merged_path = (d.env or {}).get("PATH") or os.environ.get("PATH")
        if not shutil.which(d.command, path=merged_path):
            if os.path.basename(d.command) == "claude-agent-acp":
                return {
                    "ok": False,
                    "error": "claude-agent-acp not installed — run `npm i -g @agentclientprotocol/claude-agent-acp`.",
                }
            return {"ok": False, "error": f"binary not on PATH: {d.command!r}"}
        wd = os.path.expanduser(d.workdir)
        if not os.path.isdir(wd):
            return {"ok": False, "error": f"workdir does not exist: {wd}"}

        # Real handshake — `handshake()` spawns the agent and runs ACP `initialize`
        # ONLY (no session/new, no session/load), so it's a cheap, genuinely
        # side-effect-free liveness check (#1300).
        from plugins.coding_agent.acp_client import AcpClient

        client = AcpClient(command=d.command, args=d.args, cwd=wd, env=(d.env or None), name=d.name)
        try:
            await asyncio.wait_for(client.handshake(), timeout=45)
        except asyncio.TimeoutError:
            # A binary that isn't an ACP server typically prints its usage/error to stderr
            # and then waits — indistinguishable from a slow handshake unless we show what
            # it said. The client keeps a tail of that stream for exactly this.
            tail = client.stderr_tail()
            hint = f"\nIts stderr:\n{tail}" if tail else ""
            return {"ok": False, "error": f"ACP handshake timed out — does {d.command!r} speak ACP?{hint}"}
        except Exception as exc:  # noqa: BLE001 — spawn/handshake failure → tool-visible string
            return {"ok": False, "error": f"ACP handshake failed: {type(exc).__name__}: {exc}"}
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        return {"ok": True, "detail": f"ACP handshake OK (protocol {client._protocol_version}) — {d.command}"}


ADAPTERS: dict[str, Adapter] = {a.type: a for a in (A2aAdapter(), OpenAiAdapter(), AcpAdapter())}


def delegate_types() -> list[dict]:
    """Type list + field schemas — drives the panel (PR3) and /delegate-types (PR2)."""
    return [
        {"type": a.type, "label": a.label, "blurb": a.blurb, "fields": [f.as_dict() for f in a.config_schema()]}
        for a in ADAPTERS.values()
    ]
