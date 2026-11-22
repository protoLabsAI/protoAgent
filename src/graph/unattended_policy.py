"""The unattended approval policy: allow / ask / never (ADR 0124 D5, ADR 0135 D2 and D7).

A turn nobody is watching (a scheduled fire, a watch, an inbox page, a goal drive) cannot
wait for an operator to approve an action. This module decides what such a turn may do when
a tool pauses for approval:

* **allow**: the action runs, with no decision.
* **ask**: the turn parks ONE decision that shows the exact action (tool and arguments) and
  resumes on the operator's answer.
* **never**: the action is refused, and the model is told why. This is also the default for
  anything the policy does not name: deny, do not wait.

The policy is config (``unattended.allow`` / ``unattended.ask`` / ``unattended.never``). An
entry is a tool name (``run_command``) or one action of a tool (``fleet_member_restart:start``).
The action is what the tool's approval card states in its ``action`` key.

Everything here is host-free: the turn drivers in ``server/`` mark a turn unattended with
:func:`unattended_turn`, ``graph.middleware.unattended_policy`` applies the tiers around each
tool call, and the drivers read the stamp it leaves (:func:`policy_stamp`) to answer an
allow-tier pause.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
from typing import Any

ALLOW = "allow"
ASK = "ask"
NEVER = "never"

# The key the middleware adds to an interrupt payload it has ruled on. Server-written: it is
# only ever set while a tool call runs on an unattended turn.
STAMP_KEY = "unattended_policy"

# Option ids of the decision an ask-tier action parks.
APPROVE_OPTION = "approve"
DENY_OPTION = "deny"

# ADR 0135 D2: the one action the hub may take unattended is starting a local member whose
# process is dead. ``fleet_member_restart`` is bound only on a hub, so this entry does nothing
# anywhere else. An operator entry for the tool (in any tier) replaces it.
HUB_ALLOW = ("fleet_member_restart:start",)

_MAX_ARGS_CHARS = 1500
_APPROVE_WORDS = frozenset({"approve", "approved", "yes", "y", "true", "ok"})

_unattended_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar("unattended_turn", default=False)


@contextlib.contextmanager
def unattended_turn(active: bool = True):
    """Mark the enclosed graph invocation as unattended. ``active=False`` is a no-op, so a
    driver can gate inline."""
    if not active:
        yield
        return
    token = _unattended_ctx.set(True)
    try:
        yield
    finally:
        # reset raises if a generator is torn down in a different context (as goal_turn
        # guards); the contextvar is dropped with that context anyway.
        try:
            _unattended_ctx.reset(token)
        except ValueError:
            pass


def is_unattended_turn() -> bool:
    """Whether the in-flight graph invocation runs with nobody watching."""
    return _unattended_ctx.get()


def _entries(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [s for s in (str(v).strip() for v in value if v is not None) if s]


def _is_hub() -> bool:
    try:
        from graph.workspaces.manager import is_workspace_member

        return not is_workspace_member()
    except Exception:  # noqa: BLE001 — unknown topology gets no built-in allowance
        return False


def _tiers(config) -> dict[str, list[str]]:
    return {
        NEVER: _entries(getattr(config, "unattended_never", None)),
        ASK: _entries(getattr(config, "unattended_ask", None)),
        ALLOW: _entries(getattr(config, "unattended_allow", None)),
    }


def has_action_entries(config, tool: str) -> bool:
    """Whether any tier names one ACTION of ``tool``. When it does, the tool's tier is not
    known until its approval card states the action."""
    prefix = f"{tool}:"
    return any(e.startswith(prefix) for entries in _tiers(config).values() for e in entries) or any(
        e.startswith(prefix) for e in HUB_ALLOW
    )


def resolve_tier(config, tool: str, action: str | None = None, *, hub: bool | None = None) -> str | None:
    """The tier ``tool`` (and, when known, its ``action``) is in, or ``None`` for no policy.

    An entry for the exact action beats an entry for the whole tool. At the same level the
    most restrictive tier wins (never, then ask, then allow). The hub's built-in allowance
    applies only when the operator's config says nothing about the tool. ``None`` means the
    default applies: deny."""
    tool = str(tool or "").strip()
    if not tool:
        return None
    action = str(action or "").strip()
    tiers = _tiers(config)
    keys = ([f"{tool}:{action}"] if action else []) + [tool]
    for key in keys:
        for tier in (NEVER, ASK, ALLOW):
            if key in tiers[tier]:
                return tier
    named = any(e == tool or e.startswith(f"{tool}:") for entries in tiers.values() for e in entries)
    if not named and action and f"{tool}:{action}" in HUB_ALLOW and (_is_hub() if hub is None else hub):
        return ALLOW
    return None


def describe_args(args: Any) -> str:
    """The tool call's arguments as bounded, redacted JSON for a decision or a refusal."""
    try:
        from graph.middleware.redaction import redact

        args = redact(args)
    except Exception:  # noqa: BLE001 — showing the action must not depend on the redactor
        pass
    try:
        text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        text = str(args)
    return text if len(text) <= _MAX_ARGS_CHARS else text[: _MAX_ARGS_CHARS - 1] + "…"


def _stamp(tier: str, tool: str, args: Any, action: str, call_id: str, kind: str) -> dict:
    return {"tier": tier, "tool": tool, "args": describe_args(args), "action": action, "call_id": call_id,
            "kind": kind}


def allow_payload(payload: dict, *, tool: str, args: Any, action: str = "", call_id: str = "") -> dict:
    """The tool's own approval card, stamped so the turn driver approves it."""
    kind = str(payload.get("kind") or "")
    return {**payload, STAMP_KEY: _stamp(ALLOW, tool, args, action, call_id, kind)}


def ask_payload(payload: dict, *, tool: str, args: Any, action: str = "", call_id: str = "") -> dict:
    """A ``request_decision`` payload that shows the exact action an ask-tier tool wants.

    It replaces the tool's own pause, so the existing decision path parks it, records it and
    resumes it. An approval card requires an explicit Approve / Deny pick; a plain question keeps
    its own text and takes a typed answer."""
    kind = str(payload.get("kind") or "")
    shown = describe_args(args)
    label = f"{tool} ({action})" if action else tool
    lines = [f"Tool: {tool}", f"Arguments: {shown}"]
    if action:
        lines.append(f"Action: {action}")
    for key in ("title", "detail", "question"):
        text = payload.get(key)
        if isinstance(text, str) and text.strip():
            lines.append(text.strip())
    if kind == "approval":
        question = f"Approve unattended action {label}? Arguments: {shown}"
        options = [
            {"id": APPROVE_OPTION, "label": "Approve", "description": "Run exactly this action."},
            {"id": DENY_OPTION, "label": "Deny", "description": "Do not run it."},
        ]
    else:
        question = str(payload.get("question") or payload.get("title") or f"{label} needs an answer.")
        options = []
    return {
        "kind": "decision",
        "question": question,
        "options": options,
        "requires_pick": kind == "approval",
        "context": "An unattended turn needs your approval before it acts.\n" + "\n".join(lines),
        "expires_in_minutes": None,
        "default_option": None,
        "default_action": None,
        STAMP_KEY: _stamp(ASK, tool, args, action, call_id, kind),
    }


def policy_stamp(payload: object) -> dict | None:
    """The stamp the middleware left on an interrupt payload, or ``None``."""
    if not isinstance(payload, dict):
        return None
    stamp = payload.get(STAMP_KEY)
    return stamp if isinstance(stamp, dict) and stamp.get("tier") in (ALLOW, ASK) else None


def approval_grant(stamp: dict) -> dict:
    """What an allow-tier approval card is resumed with. Reads as approved to the standard
    approval parser (``tools.fs_tools._approved``) and names the action that was allowed, so
    a tool can refuse when the state changed under it."""
    return {"approved": True, "decision": "approve", "unattended": True, "action": str(stamp.get("action") or "")}


def answer_approves(answer: object) -> bool:
    """Whether the operator's answer to an ask-tier decision approves the action. Reads the
    option id from the decisions route's ``Operator chose: Approve (approve)`` text or a bare
    answer; anything else is a denial."""
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, dict):
        if isinstance(answer.get("approved"), bool):
            return answer["approved"]
        answer = answer.get("decision") or answer.get("answer") or ""
    text = str(answer or "").strip()
    chosen = re.fullmatch(r"Operator chose: .*\(([^()]*)\)", text, re.DOTALL)
    if chosen:
        return chosen.group(1).strip().lower() == APPROVE_OPTION
    return text.lower() in _APPROVE_WORDS


def resume_value(payload: object, answer: object) -> object:
    """Translate the answer to a parked ask-tier decision into what the tool's own pause
    expects. An approval card gets an approve / deny decision; any other pause, and any
    payload this policy did not write, gets the answer unchanged."""
    stamp = policy_stamp(payload)
    if stamp is None or stamp.get("tier") != ASK or stamp.get("kind") != "approval":
        return answer
    if answer_approves(answer):
        return {"approved": True, "decision": "approve"}
    return {"approved": False, "decision": "deny", "answer": str(answer or "")}


def refusal_text(tool: str, action: str, tier: str | None) -> str:
    """The tool result an unattended turn gets for an action the policy does not permit."""
    label = f"{tool}:{action}" if action else tool
    why = (
        "it is in the never tier (unattended.never)"
        if tier == NEVER
        else "no unattended policy allows it, and the default is deny"
    )
    return (
        f"Refused by the unattended approval policy: {label} was not run because {why}. "
        "Nobody is watching this turn, so it cannot wait for an approval. Do not retry it. "
        "Continue without it and say in your reply that the operator has to run or approve this."
    )
