"""Directive-ledger tools (ADR 0127 D6) — ``record_directive``, ``list_directives``,
``fulfil_directive`` and ``revoke_directive``.

``record_directive(instance_wide=True)`` records an instance-wide standing order (ADR 0135
D13): the same operator-chat-turn check gates it, and no tool changes the reach of a
directive that already exists.

Chat-scoped: each resolves the calling chat from the injected graph state
(``tools.session._session_id_from``) — ``current_session_id()`` reads empty inside a tool
body — and refuses with a clear message when there is no chat. The ledger itself lives in
``graph.directives``.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tools.session import _session_id_from

#: The tool names, for allowlists and the prompt's capability map.
DIRECTIVE_TOOL_NAMES = frozenset({"record_directive", "list_directives", "fulfil_directive", "revoke_directive"})

_NO_CHAT = "No active chat — {name} works only during a chat turn (directives belong to a chat)."


def _operator_chat_turn(state: Any) -> bool:
    """True only on a chat turn the operator sent. The server derives this stamp from
    authenticated tier + turn origin on every pass/resume. Subagents have no stamp, and
    scheduled, watch and other automated turns carry a false one; request contextvars are
    unreliable inside a tool body."""
    return isinstance(state, dict) and state.get("operator_chat_turn") is True and not state.get("subagent_fence")


def _close(state: Any, name: str, directive_id: str, reason: str, status: str) -> str:
    from graph.directives import DirectiveError, directive_store

    session_id = _session_id_from(state)
    if not session_id:
        return _NO_CHAT.format(name=name)
    if not (reason or "").strip():
        return f"Error: {name} needs a reason — say why it is closed."
    store = directive_store()
    # An id names one ledger: this chat's first, else the instance-wide one (ADR 0135 D13).
    instance_wide = store.get(session_id, directive_id) is None and (
        store.get("", directive_id, instance_wide=True) is not None
    )
    if instance_wide and not _operator_chat_turn(state):
        # An instance-wide directive binds every turn, so an unattended turn (or a
        # subagent) must not be able to lift it for all of them.
        return (
            f"Error: {(directive_id or '').strip()} is an instance-wide directive — {name} can close it only "
            "during a chat turn with the operator; this turn did not come from the operator"
        )
    try:
        d = store.close(
            session_id, directive_id, status=status, reason=reason, closed_by="agent", instance_wide=instance_wide
        )
    except DirectiveError as exc:
        return f"Error: {exc}"
    return f"Directive {d.id} is now {d.status}: {d.close_reason}"


def build_directive_tools() -> list:
    """The four ledger tools. Always bound for the lead agent: approvals must never depend
    on a config flag to survive compaction."""

    @tool
    def record_directive(
        quote: str,
        conditions: str = "",
        scope: str = "",
        links: list[str] | None = None,
        instance_wide: bool = False,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Record a standing instruction the operator gave in THIS chat.

        A directive changes what you may or must do on a LATER turn without being told
        again: a gate ("send it only after I've reviewed the draft"), a hold or pause, a
        prohibition ("don't contact them until I say so"), a limit ("keep spend under
        $500"), or a deferred action ("once the results are in, write up a summary").
        Test: would a later turn act differently, unprompted, because of this? If not,
        don't record it.

        NOT directives: answers to questions you asked while shaping the work (picking an
        option you offered, confirming a detail) — use the answer in the work itself, in
        whatever document, plan or record you are producing; facts, status, preferences, or
        a request you carry out this turn. An approval is a directive only for the action
        it unlocks.

        `quote` is the operator's exact words — copy them, never paraphrase; it must make
        sense on its own. `conditions` holds only the trigger or stop rules the operator
        stated ("only after both are done; if anything fails, ask me first") — never rules
        or caveats of your own. `scope` names what it covers in a few words. In `links`,
        link the tracked items the directive covers — GitHub issues/PRs as `owner/repo#N`,
        board card ids (`bd-…`), scheduler job ids, task ids — so it closes itself once
        every linked item is finished. Link whenever the work is tracked; otherwise call
        fulfil_directive the turn it is done. Recorded directives appear under STANDING
        DIRECTIVES in <working_state> every turn, even after compaction. Returns the new
        directive id.

        `instance_wide=True` records a standing order for the whole instance: it then
        appears in every chat and every unattended (scheduled or watch) turn, not only
        this chat. Set it only when the operator's words this turn say it applies
        everywhere or always; otherwise leave it False. A directive's reach is fixed when
        it is recorded — never re-record an existing chat directive as instance-wide on
        your own.
        """
        from graph.directives import DirectiveError, directive_store

        session_id = _session_id_from(state)
        if not session_id:
            return _NO_CHAT.format(name="record_directive")
        if not _operator_chat_turn(state):
            if instance_wide:
                return (
                    "Error: an instance-wide directive is recorded only from what the operator says in a "
                    "chat turn; this turn did not come from the operator, so nothing was recorded"
                )
            return (
                "record_directive only records what the operator says in a chat turn; "
                "this turn did not come from the operator"
            )
        try:
            d = directive_store().record(
                session_id, quote, conditions=conditions, scope=scope, links=links, instance_wide=instance_wide is True
            )
        except DirectiveError as exc:
            return f"Error: {exc}"
        if d.instance_wide:
            linked = f", linked to {', '.join(e['ref'] for e in d.links)}" if d.links else ""
            return (
                f"Recorded instance-wide directive {d.id}{linked}. It is in force in every chat and every "
                "unattended turn until it is fulfilled or revoked in a chat turn with the operator, or the "
                "operator withdraws it."
            )
        if d.links:
            refs = ", ".join(e["ref"] for e in d.links)
            return (
                f"Recorded directive {d.id}, linked to {refs}. It fulfils itself once every "
                "linked item finishes; fulfil_directive/revoke_directive still close it sooner."
            )
        return (
            f"Recorded directive {d.id}. It stays in force until you call fulfil_directive or "
            "revoke_directive, or the operator revokes it."
        )

    @tool
    def list_directives(
        include_closed: bool = False,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """List the operator's directives recorded in THIS chat, plus any instance-wide
        ones (in force in every chat and unattended turn).

        In-force ones by default; `include_closed=True` adds fulfilled and revoked ones with
        who closed them and why. Check this before deciding you lack approval for something.
        """
        from graph.directives import directive_store, render_ledger

        session_id = _session_id_from(state)
        if not session_id:
            return _NO_CHAT.format(name="list_directives")
        store = directive_store()
        items = store.list(session_id, include_closed=bool(include_closed))
        shared = store.list("", include_closed=bool(include_closed), instance_wide=True)
        if not items and not shared:
            return (
                "No directives in force in this chat." if not include_closed else "No directives recorded in this chat."
            )
        return render_ledger(items, shared)

    @tool
    def fulfil_directive(
        directive_id: str,
        reason: str,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Close a directive in THIS chat as done (`fulfilled`), with a one-line `reason`.

        Call it once the instruction has been carried out, so it stops riding every turn.
        An instance-wide directive can be closed only during a chat turn with the operator.
        """
        from graph.directives import STATUS_FULFILLED

        return _close(state, "fulfil_directive", directive_id, reason, STATUS_FULFILLED)

    @tool
    def revoke_directive(
        directive_id: str,
        reason: str,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Close a directive in THIS chat as withdrawn (`revoked`), with a one-line `reason`.

        Only when the operator has withdrawn or replaced it — quote what they said in
        `reason`. Never revoke one on your own judgment.
        """
        from graph.directives import STATUS_REVOKED

        return _close(state, "revoke_directive", directive_id, reason, STATUS_REVOKED)

    return [record_directive, list_directives, fulfil_directive, revoke_directive]
