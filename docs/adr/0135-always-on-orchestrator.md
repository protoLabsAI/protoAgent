# ADR 0135 — The always-on orchestrator: the hub as the fleet's chief of staff

- Status: Accepted (2026-11-18, Josh: "as suggested"). Phase 0 is config on the hub; phases 1–4 are tracked on epic #5532 and are not yet boarded.
- Date: 2026-11-18
- Deciders: Josh Mabry; protoAgent maintainers
- Amends: [ADR 0007](./0007-directory-aware-operator-agent.md) (the operator persona may ship as an opt-in archetype, not only as a fork).
- Builds on: [ADR 0042](./0042-fleet-supervisor-unified-console.md) (fleet supervisor), [ADR 0067](./0067-standalone-watch-primitive.md) (watches), [ADR 0079](./0079-autonomous-operating-model.md) (operating model), [ADR 0124](./0124-per-caller-run-command-approval-policy.md) (approval policy, Proposed), [ADR 0129](./0129-work-consolidation.md) (Work), [ADR 0131](./0131-work-knows-its-conversation.md) (observe-only supervision; names the health loop as out of scope).

## Context

Josh, 2026-11-18: the main-instance agent should be "the quintessential always on agent, monitoring and managing the operators needs and the fleet, making sure that things are running correctly and that any issues are escalated to the user or handled if it can." It is also the agent we demo from.

**What the hub is today** (a fleet hub on v0.249.0 with 10 local members and 3 remotes):
- Its SOUL calls it the fleet orchestrator, but it only acts when asked. It has no scheduled jobs, no watches, no directives and an empty decisions queue. Every standing job in the fleet belongs to a member.
- Its model can call `delegate_to` and `list_agents`. `fleet_diagnostics` exists and is off by default.
- Its SOUL forbids restarting a member unless asked.
- Its friction log records a delegate badge that stayed green while every dispatch timed out. A failed dispatch still does not degrade the badge.

**What the runtime ships** (main at `ecb40496c`):
- Present: cron and one-shot schedules with missed-fire recovery, watches, a priority inbox, bus-triggered turns, the decisions queue, the four-group Work view, task-stall sweeps, compaction that preserves directives.
- Absent:
  - an agent heartbeat (ADR 0007 §8 leaves it to forks);
  - a fleet health loop or alert path (ADR 0131, out of scope), and any restart of a crashed member (`fleet.autostart` runs only at hub boot);
  - proactive delivery beyond new decisions (`notify_chat_surfaces` has one caller);
  - server-side push, quiet hours, a spend cap for unattended turns;
  - instance-wide standing orders (directives are per chat);
  - a persistent home conversation (ADR 0129 D6 made `system:activity` a feed).
- Agent-created cron jobs silently expire after 7 days (`src/tools/scheduler_tools.py`).
- The scheduler already keeps an `occurrences` ledger and outcome counters, but the operator cannot see them and nothing alerts on a missed slot.
- An unattended turn auto-answers up to three approval interrupts with "no interactive operator available", then gives up. Only `request_decision` parks and waits.

**Outside norm** (OpenAI dots, xAI Grok Bot, Nous Hermes Agent, OpenClaw, Claude Duet, LangChain ambient agents):
- Table stakes: a cheap periodic check plus cron plus event triggers; a silent path when there is nothing to report; owner-only messaging with a morning brief; tiered approvals with a never-list; unattended contexts that deny instead of waiting; bounded, editable memory; visible cost.
- The interrupt taxonomy is notify / question / review, backed by an inbox because chat pings get lost.
- Documented failures: full-context heartbeats that burn hundreds of dollars a day, exposed gateways, unvetted skill marketplaces, scheduled jobs that confirm but never fire, notification overload.
- No product found detects a stuck agent, restarts it and escalates to a human. Grok Bot's Chief-of-Staff Bot coordinates work without documented health checks; enterprise guardian agents watch policy, not liveness.

## Decision

**D1. The hub is the fleet's chief of staff.** It detects, routes, escalates and proves what ran. It does not do members' work and does not take over their duties.

**D2. One unattended action.** The hub may restart a local member whose process is dead, with bounded attempts, an audit entry and a notice. It never restarts a member the operator stopped on purpose and never acts on a remote. Every other consequential action becomes a decision that shows the exact action.

**D3. Lanes stay where they are.** Code fixes and friction triage stay with the project-manager member. Infrastructure monitoring stays with the member that owns it. Members keep their own schedules; the hub never schedules on a member's behalf.

**D4. An opt-in archetype in core, not a fork.** The orchestrator ships as a catalog archetype plus generic core seams, all off by default. This amends ADR 0007, which kept the operator persona out of the template. The reason is that the demo agent must be the thing people install.

**D5. The heartbeat is cheap and quiet.** The fleet check and the morning brief are ordinary scheduled jobs. Each runs in its own small job session on the aux model and replies `NO_UPDATE` when nothing changed. No job carries the operator's full chat context.

**D6. Reaching the operator.** Proactive output is one of three kinds: notify, question (a decision) or review (approve an exact action). Delivery goes to the owner only. A chat-channel DM is the first transport; server-side push follows. Quiet hours hold low-importance notices for a digest.

**D7. Trust is built in, not added later.** Unattended turns get a spend cap, a stated allow / ask / never policy that defaults to deny (ADR 0124), and a visible run history with a missed-slot alert.

**D8. Extend the existing seam; no parallel logic and no stopgaps.** Every slice names the seam it extends, and a slice that would add a second mechanism beside an existing one is rejected at review:

| Need | The one seam |
|---|---|
| Member health and restart | the supervisor's probes, `_alive` and the autostart start path; the delegate health record reads the same result for a delegate that is also a member |
| Reading a member | `_fetch_member_json` (auth swap, timeout, failure containment) and the `fleet_diagnostics` bounds and redaction |
| Paging the agent | the inbox store and its dedup key |
| Reaching the operator | `notify_chat_surfaces` and the adapter `notify` hook; a question is `request_decision`; push is one more adapter |
| Standing work | the scheduler, and the ADR 0129 D2 follow-up record when it lands |
| Standing orders and approvals | the directive store, with ADR 0129 D4 finished on the same path |
| Unattended policy | ADR 0124, replacing the fixed auto-answers |
| Spend | the usage store (ADR 0122) and the existing autonomous-origin classification |
| Run history | the scheduler's `occurrences` ledger |

Nothing ships as a workaround for a later phase. In particular the hub does not poll member health from a scheduled model turn while #5533 is unbuilt: liveness is the supervisor's job, in code.

## Phases

| Phase | Scope | Tracking |
|---|---|---|
| 0 | Config only on the hub, limited to what is already the end state: SOUL with standing duties, `fleet_diagnostics` on, a morning brief | this ADR |
| 1 | Fleet health loop and crash watchdog; agent fleet tools; honest delegate health; no silent 7-day expiry | #5533, #5534, #5535, #5536 |
| 2 | Notify / question / review delivery; quiet hours and digests; server-side push | #5537, #5538, #5539 |
| 3 | Spend cap; unattended approval policy; instance-wide standing orders; run history | #5540, #5541, #5542, #5543 |
| 4 | Orchestrator archetype; home conversation; demo script | #5544, #5545, #5546 |

Phase 0 goes first because it costs nothing and shows which gaps hurt before core work is boarded. It contains no stand-in for a later phase. Console UI in phases 2–4 needs a design-system spec and preview approved before a card is cut.

## Consequences

- The hub gains standing work, so its cost is no longer zero when idle. D5 and the spend cap bound it.
- Until #5533 ships, the hub does not notice a member crash on its own and cannot restart one unattended. The SOUL does not claim otherwise.
- Until #5536 ships, a standing job is created by the operator (console or API), which sets no expiry. An agent-created one lapses after 7 days.
- Until #5537 ships, a proactive result reaches the operator's phone only if the job's own turn sends it with a chat-plugin tool.
- ADR 0131 D5 stays in force for board work: supervision of cards is observe-only. D2 here covers process liveness only.

## Alternatives considered

- **Config and SOUL only.** No code, but no delivery with the console closed, no restarts and no spend cap. Kept as phase 0, not as the end state.
- **A fork**, as ADR 0007 intended. Rejected: fork upkeep, and the demo agent stops being the template.
- **A first-party plugin holding all of it.** Rejected as the primary path: the health loop, delivery seam, spend cap and approval policy are host concerns. Kept as the fallback if core seams stall.
- **Hermes Agent or OpenClaw as the always-on brain.** Rejected: the external-runtime path loses goals, telemetry and the plugin surface, and OpenClaw's security record is poor.
