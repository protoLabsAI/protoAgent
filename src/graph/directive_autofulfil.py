"""Auto-fulfil directives whose linked work finished elsewhere (ADR 0129 D4/Q2).

A directive only used to close when the agent called ``fulfil_directive``, which agents
forget: two in-force directives outlived every issue they covered (bd-nxkeo evidence —
one outlived #877 closing, one outlived all seven of #879–#884 and #873). Now a directive
carries ``links`` to the work its approval covers (``graph.directives``), and this module
watches the places the system already learns that work finished — the same event paths
the task auto-close (bd-4272u) rides:

* **Board card terminal** — :data:`LINK_CLOSED_TOPIC` with ``{"ref": "bd-…", "reason":
  "merged (#5146)"}`` is the generic inbound contract: anything able to say "this ref is
  finished" (a board plugin when a card goes done/cancelled, once it publishes it) fulfils
  the directives that link that ref. Nothing in core publishes it; it is the seam.
* **Task close** — ``task.changed`` with ``action: "closed"`` (the ``tasks.store``
  publish off ``task_close``/the console) resolves its ``task-…`` ref.
* **Scheduler settlement** — ``scheduler.changed``: the scheduler itself publishes
  ``finished`` (one-shot fired and removed) and ``expired`` (cron hit max_fires/TTL)
  strictly AFTER the job row is deleted (``LocalScheduler._publish_job_settled``), and
  ``canceled`` comes from the console cancel and the ``cancel_schedule`` tool. These are
  the only trustworthy "job gone" signals: ``turn.finished`` leaves ``_fire`` BEFORE
  ``_fire_and_settle`` deletes the row, so reading the job table off that event raced
  settlement and saw the finished job as still live.
* **GitHub refs** — GitHub publishes nothing here, so a cheap periodic sweep
  (:func:`sweep_directive_links`, driven by ``server.maintenance_loops``) asks ``gh``
  for the state of each ``owner/repo#N`` an in-force directive still links, and
  resolves closed ones (a merged PR reads ``closed`` on the Issues API).

Everything funnels into ``DirectiveStore.resolve_link``, which owns the all-links-done
auto-fulfil (``closed_by: "system"``, attributed reason) and the ``directive.changed``
publish the console's Work view and "Told it" rail already refresh on. Handlers are
best-effort: a store failure is logged and never breaks the publisher
(EventBus isolates handler errors too). Directives without links never match a ref, so
they are untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Generic inbound contract (shared with the task auto-close, bd-4272u): publish
#: ``{"ref": <linked id/ref>, "reason": <why>}`` here when a linked item reaches a
#: terminal state, and every in-force directive linking that ref resolves it
#: (fulfilling once all its links are done). Nothing in core publishes it — it is the
#: seam a board plugin uses when a card goes done/cancelled.
LINK_CLOSED_TOPIC = "task.link.closed"

#: GitHub Issues API states (covers PRs too; merged PRs read ``closed``); ``open`` is
#: additionally meaningful to the sweep only (a live ref it should keep re-checking).
_GH_TERMINAL = ("closed",)


def _store_of(store_or_getter: Any):
    """The live DirectiveStore: a callable is read at event time, ``None`` resolves the
    process-wide ledger (``graph.directives.directive_store``) so a graph reload that
    rebuilds stores stays covered."""
    if store_or_getter is None:
        from graph.directives import directive_store

        return directive_store()
    return store_or_getter() if callable(store_or_getter) else store_or_getter


#: ``scheduler.changed`` actions that mean the job row no longer exists. ``finished``/
#: ``expired`` are published by the scheduler itself after the delete commits
#: (``LocalScheduler._publish_job_settled``); ``canceled`` by the console cancel route and
#: the ``cancel_schedule`` tool. Non-terminal actions (added/updated/enabled/disabled/
#: restored) say nothing about the linked work and never resolve a ref.
_JOB_TERMINAL_ACTIONS = ("finished", "expired", "canceled")


def wire_directive_autofulfil(
    subscribe: Callable[[str, Callable[[dict], Any]], Callable[[], None]],
    store: Any = None,
) -> list[Callable[[], None]]:
    """Subscribe the auto-fulfil handlers on the event bus (``EventBus.subscribe_handler``
    shape). Returns the unsubscribe callables. ``store`` may be a DirectiveStore, a
    zero-arg getter, or ``None`` for the live ledger."""

    def _resolve(ref: str, reason: str) -> None:
        store_now = _store_of(store)
        if store_now is None or not ref:
            return
        try:
            store_now.resolve_link(ref, reason)
        except Exception:  # noqa: BLE001 — auto-fulfil must never break the publisher
            log.exception("[directives] link auto-fulfil failed for %r", ref)

    def on_link_closed(payload: dict) -> None:
        data = payload.get("data") or {}
        _resolve(str(data.get("ref") or "").strip(), str(data.get("reason") or "").strip())

    def on_task_changed(payload: dict) -> None:
        data = payload.get("data") or {}
        if str(data.get("action") or "") != "closed":
            return  # created/updated/deleted/restored say nothing terminal about the work
        _resolve(str(data.get("id") or "").strip(), "closed")

    def on_scheduler_changed(payload: dict) -> None:
        data = payload.get("data") or {}
        if str(data.get("action") or "") not in _JOB_TERMINAL_ACTIONS:
            return
        job_id = str(data.get("job_id") or "").strip()
        # `wait:` ids are reused per chat (a new wait supersedes the old) — never a
        # meaningful link target, so a settled or superseded one says nothing durable.
        if not job_id or job_id.startswith("wait:"):
            return
        _resolve(job_id, str(data.get("action")))

    return [
        subscribe(LINK_CLOSED_TOPIC, on_link_closed),
        subscribe("task.changed", on_task_changed),
        subscribe("scheduler.changed", on_scheduler_changed),
    ]


async def _gh_issue_state(ref: str) -> str | None:
    """``open`` / ``closed`` for a GitHub issue/PR ref via the ``gh`` CLI, or ``None``
    when it can't be read (no gh, no auth, bad ref) — unknown never resolves a link.
    The Issues API answers for PRs too, and a merged PR reads ``closed`` there, so one
    endpoint covers "issue/PR closed or merged"."""
    from graph.directives import parse_github_ref
    from tools.gh_cli import run_gh

    parsed = parse_github_ref(ref)
    if not parsed:
        return None
    owner, repo, number = parsed
    code, out, _err = await run_gh(["api", f"repos/{owner}/{repo}/issues/{number}", "--jq", ".state"])
    if code != 0 or not out:
        return None
    state = out.strip().lower()
    return state if state in (*_GH_TERMINAL, "open") else None


async def sweep_directive_links(store: Any = None, *, issue_state: Callable[[str], Any] | None = None) -> int:
    """One pass of the periodic GitHub sweep (ADR 0129 D4/Q2): resolve the issue/PR refs
    in-force directives still hold whose issue/PR has closed (or merged). Returns how
    many directives were touched. Cheap when idle: no in-force directive holds an
    unresolved GitHub ref → returns before any ``gh`` call. ``issue_state`` is the
    fetch seam (defaults to ``gh``)."""
    store_now = _store_of(store)
    if store_now is None:
        return 0
    refs: list[str] = []
    seen: set[str] = set()
    for d in store_now.linked_in_force():
        for entry in d.links:
            ref = entry["ref"]
            if entry.get("kind") == "github" and entry.get("state") != "done" and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    if not refs:
        return 0
    issue_state = issue_state or _gh_issue_state
    touched = 0
    for ref in refs:
        try:
            state = await issue_state(ref)
        except Exception:  # noqa: BLE001 — one unreadable ref can't stop the sweep
            log.exception("[directives] GitHub state read failed for %s", ref)
            continue
        if state in _GH_TERMINAL:
            touched += len(store_now.resolve_link(ref, state))
    return touched
