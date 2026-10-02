"""Delegate base types — the model, field schema, error and ``Adapter`` base class (ADR 0025).

Split out of ``adapters.py`` (#3831) so the per-type adapter modules (``a2a``,
``acp_adapter``) and the ``ADAPTERS`` registry in ``adapters`` can all import the shared
types without an import cycle. ``adapters`` re-exports every name here — import from
either.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


class DelegateError(Exception):
    """A dispatch/parse failure. The caller turns it into a tool error string.

    ``kind`` says WHAT failed, because callers act on the difference. Only
    ``unreachable`` — nothing accepted a connection — means the target may simply be a
    stopped local member worth starting. A timeout or an HTTP error means something DID
    answer, and restarting an agent underneath a slow-but-live turn is a footgun, not a
    fix. Left empty for the failures nobody dispatches on.
    """

    def __init__(self, message: str, *, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


KIND_UNREACHABLE = "unreachable"
# The peer TOOK the message and we gave up waiting for the answer — distinct from
# unreachable, which means it never got one. The difference decides whether an a2a
# conversation's continuity survives the failure (see ``conversations.forget_one``):
# a peer that never received the message still describes the conversation we remember;
# one that is mid-turn has moved it somewhere this side has no record of.
KIND_TIMEOUT = "timeout"
# The peer took the work and is STILL doing it when this side's wait ran out (no-progress
# bound or the call's own timeout). Not a failure of the peer: the error names the task id
# and how to collect it (#3700/#3775), and the foreground tool reports it as "not finished
# yet" rather than "failed".
KIND_STILL_RUNNING = "still_running"


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
    poll_timeout_s: float = 300.0  # a2a: max seconds to wait without observed task progress

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
    # Default 1800s (30 min), not 600 (#3091): implementation-shaped ACP delegates
    # (a full TDD cycle, venv setup, a CI gate run) routinely need more than 10 min.
    # Still per-delegate configurable, and overridable per-call via delegate_to(timeout=…).
    timeout_s: float = 1800.0
    permissions: str = "auto"
    allow_kinds: list[str] = field(default_factory=list)
    deny_kinds: list[str] = field(default_factory=list)
    # Per-invocation values. The registry sets these on an immutable dataclass copy;
    # they are never parsed from or persisted to delegate config. ``conversation_key``
    # names one continuing conversation with this delegate — an ACP session for a coding
    # agent, the peer-assigned A2A ``contextId`` for an a2a peer (#3360);
    # ``permissions_ceiling`` stays ACP-only, being the one type that can enforce it.
    # ``origin_session_id`` is the chat SESSION this dispatch originated from — recorded
    # BESIDE (never inside) the resolved ``conversation_key`` so a delete that knows only the
    # session can forget the a2a context later (#3362). A resolved key can't be reversed into
    # its session (a custom thread-id resolver mints it from request metadata), so it has to
    # ride explicitly; ``""`` for a caller that doesn't know it, which records no origin.
    conversation_key: str = ""
    permissions_ceiling: str = ""
    origin_session_id: str = ""
    confirm: bool = False
    # acp managed git (ADR 0076): the framework owns branch/commit/push/PR; the
    # coder edits files only. Off by default — non-worktree setups keep the old
    # coder-owns-git mode.
    manage_git: bool = False
    base_branch: str = "main"
    branch_prefix: str = ""  # empty ⇒ the delegate's name
    # acp, unmanaged: append a change summary (stat + capped unified diff) to the reply of
    # a ``delegate_to(project=…)`` dispatch into a git project. Operator opt-out per
    # delegate. ``capture_diff`` is the per-invocation switch the registry sets on its
    # ``dataclasses.replace`` copy (never parsed from or persisted to config), and
    # ``project_name`` labels that summary.
    return_diff: bool = True
    capture_diff: bool = False
    project_name: str = ""


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
