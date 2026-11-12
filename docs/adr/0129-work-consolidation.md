# ADR 0129 — Work consolidation: five things a person tracks, and an agent tool surface to match

- Status: Proposed (2026-11-11). Josh approved the direction ("hold and fold into the redesign"). The open questions below are his to answer.
- Date: 2026-11-11
- Deciders: Josh Mabry; protoAgent maintainers
- Implementation: not started. The console redesign is a designSystem spec (pending approval); slices follow below.
- Amends: [ADR 0127](./0127-compaction-fidelity.md) D6 (directive ledger). Touches the scheduler, watch, task, goal and decision tool families.

Code citations are `origin/main` after v0.236.0. **(unverified)** marks inference not yet confirmed from code.

## Context

The console's Work panel has eight sections (`apps/web/src/work/sections/`): decisions, tasks, background, schedules, watches, goals, directives and activity. Each one mirrors a subsystem. The agent sees the same split as about two dozen tools:

| Concept | Agent tools |
|---|---|
| Schedules | `schedule_task`, `list_schedules`, `cancel_schedule`, `wait` (`tools/scheduler_tools.py`) |
| Watches | `create_watch`, `update_watch`, `clear_watch`, `list_watches` |
| Tasks | `task_create`, `task_list`, `task_update`, `task_close` |
| Goals | `set_goal`, `update_goal_plan`, `abandon_goal` |
| Decisions | `request_decision`, `withdraw_decision`, `ask_human` |
| Directives | `record_directive`, `list_directives`, `fulfil_directive`, `revoke_directive` (`tools/directive_tools.py`) |
| Inbox | `check_inbox` |
| Background jobs | subagent delegation run in the background (unverified: exact tool names) |

Josh, 2026-11-11: "this entire work/activity panel is a bit much. schedules, tasks, goals, watches, directive. like its just an overload for a user and the agent."

Evidence from the same day:

- **A directive that says nothing.** mythxEngineer answered a decision ("run the #877 merge queue?") by clicking an option. It then hand-recorded directive `dir-cfd234` with the quote `"confirmed"`, which is a word the operator never typed. The console card leads with that quote. What it actually authorizes is in small print.
- **The decision and the directive duplicate each other.** The answered decision already records who approved what, and when.
- **A watch contradicted the newer approval.** A watch prompt still said "never merge #864–#871" after the operator confirmed merging them. The agent had to notice the conflict and resolve it by itself.
- **Raw internal text reached the operator.** Board pages, delivered as priority-now inbox items, rendered as "undelivered page" alerts full of internal diagnosis (projectBoard-plugin#735).
- **Decisions in a background session went unseen.** One raised in `system:activity` sat for 70 minutes, and answering it looked frozen (#5099).

## Decision

### D1 — Five groups, for the operator and the agent alike

Everything the agent is tracking falls into one of five groups:

1. **Needs me**: decisions, and items that need a human (for example, a stuck delivery). This is the only group that asks for attention.
2. **Doing now**: background jobs, tasks in progress, the active goal.
3. **Coming up**: anything that will run later, whether set off by a time (schedule, `wait`) or by a condition (watch).
4. **What I told it**: the operator's standing instructions and approvals.
5. **What happened**: the activity timeline.

The console's Work panel renders these five groups with one row primitive, using the kind (schedule, watch, goal, job…) as a tag. The in-chat status rail summarizes groups 1–4 with the same vocabulary and the same counts. The visual design is a designSystem spec that Josh approves before any build.

### D2 — One "follow up later" primitive

Schedules, watches and `wait` collapse into one concept: a **follow-up** with a trigger, either a time or a condition, plus a prompt and an owner chat. The agent gets one tool family (create, list, update, cancel) instead of three. Existing schedule and watch records migrate. The old tool names stay as thin aliases for one release so forks and plugins can move, then they are removed.

### D3 — A goal is a job with a finish line

A goal stops being a separate subsystem from the operator's point of view. It shows in **Doing now** as a job with its completion criteria and its plan. The goal loop's internals (ADR 0073) are unchanged. `set_goal` and `update_goal_plan` stay, but their records surface through the same row and inspector as other jobs.

### D4 — Approvals come from answers, not agent paraphrase (amends ADR 0127 D6)

- **An answered decision is a standing approval, recorded by the server.** Its headline is the decision's question and the chosen option, and it links to the decision. The agent does not call `record_directive` for a decision answer.
- **`record_directive` stays for free-text instructions.** The server rejects a `quote` that does not appear verbatim in the operator's messages for that turn.
- **The display leads with what is approved.** The headline is the scope. The operator's words show beneath as provenance ("You said 'go for it' · 16:07").
- **A directive tied to a bounded job closes itself** when the agent marks the job done (open question Q2).

### D5 — Fewer, clearer tools for the agent

The prompt's capability map groups the tools under the same five headings, and each heading gets one paragraph on when to use it. The target is a third fewer tools in the always-bound set. Aliases do not count toward the target.

## Consequences

- **Good:** one mental model for operator and agent. Fewer wrong-tool calls and fewer contradictory records (a watch versus an approval). Work becomes scannable, and decisions stop hiding in background sessions.
- **Cost:** migrating schedule and watch records. A release cycle of aliases. Plugins that register Work sections (`registerWorkSection`) need to choose a group (unverified: which plugins do).
- **Risk:** merging schedule and watch semantics could lose a capability one of them has (for example, a watch's `check` function). S1 audits both before any code change.

## Slices

- **S0:** the designSystem spec and preview for the five-group Work panel plus the rail. Josh approves. The held Activity cards (bd-ek2ye, bd-tn5g6, bd-0ohpa, bd-50ejc) are replaced by S0's build cards.
- **S1:** audit schedule, watch and `wait` (fields, triggers, delivery, persistence). Write the follow-up record shape and the migration.
- **S2:** console. Five groups on today's data, mapped client-side, with no backend dependency.
- **S3:** D4 approvals. Server-recorded directives from decision answers, quote verification, and the scope-first display.
- **S4:** D2 follow-up primitive, plus aliases and the migration.
- **S5:** D5 prompt capability map and tool-count reduction. Remove the aliases one release after S4.

Independent fixes in flight that this ADR keeps: #5099 (a decision answer returns as soon as it is consumed) and projectBoard-plugin#735 (human page text, and pages retire when their card does).

## Open questions (Josh)

- **Q1:** Do tasks (`task_create`…) stay as a separate kind in **Doing now**, or fold into jobs? They overlap with board cards on agents that run a board.
- **Q2:** Should approvals tied to a bounded job close themselves when the job is done, or stay in force until withdrawn?
- **Q3:** Should **What happened** (activity) live in Work at all, or only as its own view?
