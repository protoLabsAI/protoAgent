"""Directive-ledger tools (ADR 0127 D6) — ``record_directive``, ``list_directives``,
``fulfil_directive`` and ``revoke_directive``.

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


def _close(state: Any, name: str, directive_id: str, reason: str, status: str) -> str:
    from graph.directives import DirectiveError, directive_store

    session_id = _session_id_from(state)
    if not session_id:
        return _NO_CHAT.format(name=name)
    if not (reason or "").strip():
        return f"Error: {name} needs a reason — say why it is closed."
    try:
        d = directive_store().close(session_id, directive_id, status=status, reason=reason, closed_by="agent")
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
        """
        from graph.directives import DirectiveError, directive_store

        session_id = _session_id_from(state)
        if not session_id:
            return _NO_CHAT.format(name="record_directive")
        # The server derives this stamp from authenticated tier + turn origin on every
        # pass/resume. Subagents have no stamp; request contextvars are unreliable here.
        if not isinstance(state, dict) or state.get("operator_chat_turn") is not True or state.get("subagent_fence"):
            return (
                "record_directive only records what the operator says in a chat turn; "
                "this turn did not come from the operator"
            )
        try:
            d = directive_store().record(session_id, quote, conditions=conditions, scope=scope, links=links)
        except DirectiveError as exc:
            return f"Error: {exc}"
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
        """List the operator's directives recorded in THIS chat.

        In-force ones by default; `include_closed=True` adds fulfilled and revoked ones with
        who closed them and why. Check this before deciding you lack approval for something.
        """
        from graph.directives import directive_store, render_ledger

        session_id = _session_id_from(state)
        if not session_id:
            return _NO_CHAT.format(name="list_directives")
        items = directive_store().list(session_id, include_closed=bool(include_closed))
        if not items:
            return (
                "No directives in force in this chat." if not include_closed else "No directives recorded in this chat."
            )
        return render_ledger(items)

    @tool
    def fulfil_directive(
        directive_id: str,
        reason: str,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Close a directive in THIS chat as done (`fulfilled`), with a one-line `reason`.

        Call it once the instruction has been carried out, so it stops riding every turn.
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
