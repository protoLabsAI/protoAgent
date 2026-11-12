"""Auto-close tasks whose linked work finished elsewhere (ADR 0129 Q1/Q2).

A task only used to close when the agent called ``task_close`` in the chat that
created it — five tasks sat ``in_progress`` for days while the work shipped through
board merges, other sessions and A2A requests. Now a task carries ``links`` to the
work that delivers it (``tasks.store``), and this module watches the places the
system already learns that work finished:

* **Board card terminal** — a board plugin (or any other owner of a linked item)
  publishes :data:`TASK_LINK_CLOSED_TOPIC` with ``{"ref": "bd-…", "reason":
  "merged (#5146)"}`` when a card reaches done/cancelled. The topic is the generic
  inbound contract: anything able to say "this ref is finished" can close the tasks
  that link it.
* **Scheduler settlement** — ``turn.finished`` (origin ``scheduler``) marks a fire
  settled; the job row is read back, and a job that no longer exists (one-shot
  settled, cron expired) resolves its ref. The scheduler publishes it only after
  the row settles — ``_settle_task`` orders it so on the task-id path, and
  ``_fire_and_settle`` emits the no-task-id fallback event after the one-shot
  delete / cron expiry — so this inline read-back is race-free. ``scheduler.changed``
  with ``action: "canceled"`` (console cancel, ``cancel_schedule`` tool) resolves it
  as canceled.
* **Directive close** — ``directive.changed`` is read back against the directive
  ledger; a directive now ``fulfilled``/``revoked`` resolves its ``dir-…`` ref.
* **PR links** — GitHub publishes nothing here, so a cheap periodic sweep
  (:func:`sweep_task_links`, driven by ``server.maintenance_loops``) asks ``gh`` for
  the state of each PR URL an open task still links, and resolves merged/closed ones.

Everything funnels into ``TaskStore.resolve_link``, which owns the all-links-done
auto-close and the ``task.changed`` publish. Handlers are best-effort: a lookup or
store failure is logged and never breaks the publisher (EventBus isolates handler
errors too). Tasks without links never match a ref, so they are untouched.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

log = logging.getLogger("protoagent.tasks")

#: Generic inbound contract: publish ``{"ref": <linked id/URL>, "reason": <why>}`` here
#: when a linked item reaches a terminal state, and every open task linking that ref
#: resolves it (closing once all its links are done). Board plugins publish it when a
#: card goes done/cancelled.
TASK_LINK_CLOSED_TOPIC = "task.link.closed"

_DIRECTIVE_TERMINAL = ("fulfilled", "revoked")

#: ``gh pr view --json state`` answers; anything else is treated as unknown.
_PR_TERMINAL = ("merged", "closed")


def _store_of(store_or_getter: Any):
    """The live TaskStore: a callable is read at event time (the server passes
    ``lambda: STATE.tasks_store`` so a rebuilt store stays covered)."""
    return store_or_getter() if callable(store_or_getter) else store_or_getter


def _default_get_job(job_id: str):
    """The scheduler's job row, or raise when no scheduler is wired — "can't know"
    must never read as "job gone" (that would close a task on a boot race)."""
    from runtime.state import STATE

    getter = getattr(getattr(STATE, "scheduler", None), "get_job", None)
    if not callable(getter):
        raise LookupError("no scheduler wired")
    return getter(job_id)


def _default_get_directive(session_id: str, directive_id: str):
    from graph.directives import directive_store

    return directive_store().get(session_id, directive_id)


def wire_task_autoclose(
    subscribe: Callable[[str, Callable[[dict], Any]], Callable[[], None]],
    tasks_store: Any,
    *,
    get_job: Callable[[str], Any] | None = None,
    get_directive: Callable[[str, str], Any] | None = None,
) -> list[Callable[[], None]]:
    """Subscribe the auto-close handlers on the event bus (``EventBus.subscribe_handler``
    shape). Returns the unsubscribe callables. ``tasks_store`` may be the store or a
    zero-arg getter; ``get_job`` / ``get_directive`` default to the live scheduler and
    directive ledger and exist as seams for tests."""
    get_job = get_job or _default_get_job
    get_directive = get_directive or _default_get_directive

    def _resolve(ref: str, reason: str) -> None:
        store = _store_of(tasks_store)
        if store is None or not ref:
            return
        try:
            store.resolve_link(ref, reason)
        except Exception:  # noqa: BLE001 — auto-close must never break the publisher
            log.exception("[tasks] link auto-close failed for %r", ref)

    def on_link_closed(payload: dict) -> None:
        data = payload.get("data") or {}
        _resolve(str(data.get("ref") or "").strip(), str(data.get("reason") or "").strip())

    def on_directive_changed(payload: dict) -> None:
        data = payload.get("data") or {}
        directive_id = str(data.get("id") or "").strip()
        if not directive_id:
            return  # a whole-ledger event (chat deleted) names no directive
        try:
            directive = get_directive(str(data.get("session_id") or ""), directive_id)
        except Exception:  # noqa: BLE001 — a ledger read failure is not a close
            log.exception("[tasks] directive lookup failed for %r", directive_id)
            return
        status = getattr(directive, "status", "")
        if status not in _DIRECTIVE_TERMINAL:
            return  # unknown directive, or still in force
        reason = getattr(directive, "close_reason", None)
        _resolve(directive_id, f"{status}: {reason}" if reason else status)

    def on_scheduler_changed(payload: dict) -> None:
        data = payload.get("data") or {}
        if str(data.get("action") or "") != "canceled":
            return
        _resolve(str(data.get("job_id") or "").strip(), "canceled")

    def on_turn_finished(payload: dict) -> None:
        data = payload.get("data") or {}
        if data.get("origin") != "scheduler":
            return
        job_id = str(data.get("trigger") or "").strip()
        # `wait:` ids are reused per chat (a new wait supersedes the old) — never a
        # meaningful link target, so a settled one says nothing durable.
        if not job_id or job_id.startswith("wait:"):
            return
        try:
            job = get_job(job_id)
        except Exception:  # noqa: BLE001 — "can't know" must never become "job gone"
            return
        if job is not None:
            return  # recurring job still live — this fire settled, the job didn't
        _resolve(job_id, "failed" if data.get("ok") is False else "finished")

    return [
        subscribe(TASK_LINK_CLOSED_TOPIC, on_link_closed),
        subscribe("directive.changed", on_directive_changed),
        subscribe("scheduler.changed", on_scheduler_changed),
        subscribe("turn.finished", on_turn_finished),
    ]


async def _gh_pr_state(url: str) -> str | None:
    """``merged`` / ``closed`` / ``open`` for a PR URL via the ``gh`` CLI, or ``None``
    when it can't be read (no gh, no auth, bad URL) — unknown never resolves a link."""
    from tools.gh_cli import run_gh

    code, out, _err = await run_gh(["pr", "view", url, "--json", "state"])
    if code != 0 or not out:
        return None
    try:
        state = str(json.loads(out).get("state") or "").strip().lower()
    except (ValueError, AttributeError):
        return None
    # The terminal states come from the module constant; `open` is additionally
    # meaningful HERE only (a live PR the sweep should keep re-checking).
    return state if state in (*_PR_TERMINAL, "open") else None


async def sweep_task_links(tasks_store: Any, *, pr_state: Callable[[str], Any] | None = None) -> int:
    """One pass of the periodic PR sweep (ADR 0129 Q1): resolve the PR links open
    tasks still hold whose PR has merged or closed. Returns how many issues were
    touched. Cheap when idle: no open task holds an unresolved PR link → returns
    before any ``gh`` call. ``pr_state`` is the fetch seam (defaults to ``gh``)."""
    store = _store_of(tasks_store)
    if store is None:
        return 0
    from tasks.store import parse_links

    urls: list[str] = []
    seen: set[str] = set()
    for row in store.list(include_closed=False):
        for entry in parse_links(row):
            ref = entry["ref"]
            if entry.get("kind") == "pr" and entry.get("state") != "done" and ref not in seen:
                seen.add(ref)
                urls.append(ref)
    if not urls:
        return 0
    pr_state = pr_state or _gh_pr_state
    touched = 0
    for url in urls:
        try:
            state = await pr_state(url)
        except Exception:  # noqa: BLE001 — one unreadable PR can't stop the sweep
            log.exception("[tasks] PR state read failed for %s", url)
            continue
        if state in _PR_TERMINAL:
            touched += len(store.resolve_link(url, state))
    return touched
