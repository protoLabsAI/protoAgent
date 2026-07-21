# ADR 0090 — Console-created goals drive in a dedicated chat tab (attach / detach)

**Status:** Accepted

**Implementation:** Shipped. FE: `apps/web/src/app/goalTab.ts` (the create-in-a-tab flow),
the chat-store goal-kickoff seam + `ChatSessionSlot` auto-kickoff and the goal-tab close-guard
in `apps/web/src/chat/ChatSurface.tsx`, the goal **detail drawer**
(`apps/web/src/app/GoalDetailDrawer.tsx`). BE: `kick` param on `POST /api/goals`, new
`POST /api/goals/{sid}/rearm` and `/resume`, `plan` on `GET /api/goals/{sid}`, and
`?close_tasks` on `DELETE /api/goals/{sid}` (`operator_api/`, `graph/goals/controller.py`).
Covered by `tests/test_goal_lifecycle.py`, `tests/test_console_handlers.py`,
`tests/test_operator_api_routes.py`, and `apps/web/e2e/work-overview.spec.ts`. **Follow-up:**
re-opening a *detached* goal back into a tab (detached goals are Goals-panel-only today).

**Relates to:** [ADR 0079](0079-autonomous-operating-model.md) (the OODA drive loop — this is
its console surface), [ADR 0073](0073-goal-completion-contracts.md) (the contract the drawer
reads back), [ADR 0066](0066-goal-trust-operator-channel.md) (the operator-tier `/api/goals`
channel), [ADR 0028](0028-plugin-goal-verifiers.md) (the verifier that decides DONE).

## Context

A goal is a self-driving outcome (ADR 0028/0073/0079): after each terminal turn a **verifier**
decides DONE, and if not, the controller re-invokes the agent with a continuation prompt until
the verifier passes or a budget is spent. That drive loop runs **inline inside a turn's
streaming generator** (`server/chat.py`) — it re-invokes and `yield`s continuation frames — so
**every iteration streams to whoever owns that turn's stream.**

Setting a goal from the console panel (`POST /api/goals`) originally just persisted the goal
state; it drove on the session's *next* turn. We then made the set **kick** an initial turn via
`sdk.run_in_session` so it started immediately (parity with the chat `/goal` set). But that turn
is **headless** — it runs in the `operator` session the operator isn't viewing. The work
happened (tasks filed, background jobs completed) but **invisibly**: the browser never opens
that turn's stream, and `ServerTurnWatch` only shows a typing indicator, with the final answer
arriving later via `chat.resumed` — not a live stream. So a panel-set goal effectively
"disappeared" into the background. The fix is to give the goal a **visible, focused surface**
to drive in.

## Decision

### D1 — A Work-panel goal drives in a dedicated, focused chat tab

Creating a goal from the Work panel opens a **new chat tab**, sets the goal on **that tab's
session** with **`kick: false`**, focuses it, and fires a **hidden** kickoff turn from the tab.
The server's iteration-0 kickoff injection re-states the goal, and the inline drive loop
**streams live into the tab**. Because the drive loop streams to the turn owner, the tab that
owns the turn sees the whole loop token-by-token. `kick` defaults to `true` for
programmatic / API callers with no UI (they keep the headless auto-start). **A goal set in
chat with `/goal` stays in that chat — unchanged.**

### D2 — Attach / detach: closing the tab keeps the goal running (or stops it)

The drive loop is bound to the tab's live stream — closing the tab aborts it. So closing a
goal-driving tab prompts a choice:

- **Keep running (detach, default)** — `POST /api/goals/{sid}/resume` kicks a **headless
  continuation** so the goal keeps driving, and the **server session is kept** (its checkpoint
  is the goal's accumulated context; purging it would strand the drive). Only the tab is
  dropped; track it in the Goals panel.
- **Stop** — `DELETE /api/goals/{sid}?close_tasks=true` clears the goal **and closes the tasks
  it filed** (the goal's session-scoped backlog, ADR 0079), then purges the session.

This cleanly separates *watch it live* from *let it run* — a closed tab never strands a goal.

### D3 — Detail + lifecycle surface

Clicking a goal opens a read-only **detail drawer**: the agent's live **plan** artifact
(`.plan.md`, rendered as markdown — `GET /api/goals/{sid}` now returns it), the completion
**contract** read-back (ADR 0073), the last verifier reason/evidence, and a per-iteration
**timeline**. From the drawer, an active goal can be given more room (**Add iterations**) and a
terminal one can be **Restarted** — both via `POST /api/goals/{sid}/rearm`.

### D4 — Operator-tier creation; the verifier stays the arbiter

Console creation goes through the operator-tier `/api/goals` channel (ADR 0066), so it accepts
**any** verifier type — the trust gate that refuses shell/eval verifiers from a *raw `/goal`
chat message* does not apply to the authenticated route. Completion is still decided solely by
the verifier (ADR 0073/0079); none of this touches that.

### D5 — Robustness alongside the surface

No-progress detection was inert for the fuzzy `llm` verifier (its free-text reason varies every
call, so the stall fingerprint never repeated); fuzzy goals now fingerprint the **plan artifact**
instead (deterministic verifiers unchanged). The best-effort kick is guarded so a scheduler
hiccup can never 500 an already-persisted goal-set.

## Consequences

- **Panel goals are front-and-center.** The operator watches the loop stream live, steers it
  (extend / restart), detaches it, or stops it — instead of it running invisibly.
- **No stranded goals.** Attach/detach means closing a tab is an explicit choice, not a silent
  drop; a detached goal keeps driving and stays visible in the Goals panel.
- **The task board is the goal's backlog, and can be closed with it** (ADR 0079 attribution made
  actionable).
- **Trade-off (accepted):** driving *from* the tab means closing it aborts the in-flight turn;
  detach re-kicks a headless continuation (a brief re-do of the aborted iteration is possible).
  This was chosen over *headless + observe*, which only shows a typing indicator and chunked
  `chat.resumed` results — not a live stream.
- **Follow-up:** re-opening a *detached* goal back into a chat tab is not built; detached goals
  are managed from the Goals panel.
