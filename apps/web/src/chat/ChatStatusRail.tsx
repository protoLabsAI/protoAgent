/** The chat status rail (work consolidation v2 §5, S6a) — the shared-count strip above the
 *  composer. One chip per non-empty Work group (Doing now · Coming up · Told it), counted by
 *  the SAME `workGroups` selector the Work view reads, with `thisChat` applied, so the chip
 *  numbers and the Work group rows cannot drift. Each chip opens one grouped details panel —
 *  a DS Popover on desktop, a DS bottom-sheet Drawer on the phone — with one section per Work
 *  group (Work's own names) and an "Open in Work" deep link. The people (cast) chips keep
 *  their mention-toggle behavior unchanged, after the group chips.
 *
 *  S6b (the v7 people behavior): chips and people share ONE row — a hidden mirror of the
 *  people row is measured (ResizeObserver on the roster, which flexes into whatever the
 *  group chips leave), and whoever doesn't fit collapses behind a `+K` chip that opens the
 *  picker ("In this chat · N") in the same desktop Popover / phone Drawer the group chips
 *  use. Draft-addressed names sort first with the picker's latest add leading them, so
 *  picking a hidden name gives it a visible slot even on a row already full of addressed
 *  names. Until the first measurement lands the conservative fallback caps apply (peopleFit).
 *
 *  S7 (fleet Needs you): the FIRST thing on the row is the Needs you marker — a primary DS
 *  Button (xs) fed by the SAME shared selector group, fleet-wide (Needs you is never chat
 *  scoped), hidden at zero and never folded behind `+K`. It opens the same grouped details,
 *  whose first section is "Needs you · N": the shared DecisionCard (answer logic and the
 *  shared decisions store, not a fork) for the first decision, one-line rows for the rest,
 *  neutral rows for undelivered human-needed pages and stopped goals (Keep going / Drop the
 *  goal). Results hold in the OPEN panel until it closes; the count stays pending-only. */
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode, RefAttributes } from "react";
import { Button, type ButtonProps } from "@protolabsai/ui/primitives";
import { Spinner, StatusDot } from "@protolabsai/ui/data";
import { Drawer, Popover } from "@protolabsai/ui/overlays";
import { Clock, Target, Users } from "lucide-react";

import { openView } from "../app/palette/nav";
import { useUI } from "../state/uiStore";
import { useIsMobile } from "../lib/useIsMobile";
import type { GoalState } from "../lib/types";
import type { DecisionRow } from "../lib/api/decisions";
import { useDecisions } from "../decisions/useDecisions";
import { sections } from "../work/sectionRegistry";
import { useWorkItems, type WorkSource } from "../work/useWorkItems";
import { flattenWorkItems, inWorkScope, jobEnded, jobKey, withSharedDecisions, workGroups, type WorkGroup } from "../work/workGroups";
import { beginThisChatVisit, endThisChatVisit } from "../work/visitScope";
import type { WorkGroupId } from "../work/tone";
import { NEEDS_YOU_SECTION_KINDS, NeedsYouSection } from "./NeedsYouSection";
import { useDirectives } from "./useDirectives";
import { runHas } from "./mentionRun";
import { fallbackCap, fitCount, orderPeople, recipientLine, spokenMoreLabel } from "./peopleFit";
import "./chat-status-rail.css";

export type RailChip = {
  id: Exclude<WorkGroupId, "needs-you">;
  /** The chip wording (§5): the group name, except "Told it" for Your instructions. */
  label: string;
  count: number;
  /** The active goal's progress, shown inside the Doing now chip ("goal 3/8"). */
  goal?: { step: number; of: number | null };
  /** The instructions ledger never loaded: no count to show, details carry "Load again". */
  problem?: boolean;
};

/** The §5 chips from the shared selector's groups: zero counts hide. Needs you is not a
 *  chip — it rides the S7 marker, first on the row, so it is excluded here. */
export function railChips(groups: WorkGroup[]): RailChip[] {
  const chips: RailChip[] = [];
  for (const group of groups) {
    if (group.id === "needs-you" || group.count === 0) continue;
    const chip: RailChip = {
      id: group.id,
      label: group.id === "instructions" ? "Told it" : group.label,
      count: group.count,
    };
    if (group.id === "doing-now") {
      const goal = group.items.find((item) => item.kind === "goal");
      if (goal) {
        const raw = goal.raw as GoalState;
        chip.goal = { step: raw.iteration ?? 0, of: raw.max_iterations ?? null };
      }
    }
    chips.push(chip);
  }
  return chips;
}

/** The complete spoken label (§5) — the phone chip shows the number only, this says it all. */
export function spokenLabel(chip: RailChip): string {
  if (chip.problem) return "Told it: couldn't check instructions in this chat. Show details";
  switch (chip.id) {
    case "doing-now": {
      const goal = chip.goal
        ? `, including the goal at step ${chip.goal.step}${chip.goal.of != null ? ` of ${chip.goal.of}` : ""}`
        : "";
      return `Doing now, ${chip.count} running in this chat${goal}. Show details`;
    }
    case "coming-up":
      return `Coming up: ${chip.count} not started in this chat. Show details`;
    case "instructions":
      return `Told it: ${chip.count} instruction${chip.count === 1 ? "" : "s"} in force in this chat. Show details`;
  }
}

/** DS Button spreads every DOM prop (React 19 ref-as-prop included) onto its `<button>`,
 *  but its props type predates ref-as-prop — widened here for the marker's focus-return ref. */
const RefButton = Button as unknown as (props: ButtonProps & RefAttributes<HTMLButtonElement>) => ReactNode;

/** The marker's complete spoken label (S7) — the phone marker shows the number alone. */
export function needsYouSpokenLabel(count: number): string {
  return count === 1 ? "1 thing needs you. Show it" : `${count} things need you. Show them`;
}

/** Where "Open in Work" lands for a group. The four-group Work sections are their own slice:
 *  once a section registers under the group id itself, the deep link targets it directly.
 *  Until then, Your instructions maps to its registered section ("directives", which already
 *  carries the v2 label) and the running/planned groups land on the Overview, whose lanes
 *  show those same rows today. */
export function workSectionForGroup(group: WorkGroupId): string {
  if (sections.has(group)) return group;
  return group === "instructions" ? "directives" : group === "needs-you" ? "decisions" : "overview";
}

/** Deep link to a Work group under the `thisChat` scope the chip counted with. The scope
 *  rides the VISIT (work/visitScope), never the saved surface state: the saved filters are
 *  what every other Work entry restores, and one rail click must not rewrite them.
 *  (Needs you deliberately doesn't scope — it is never filtered.) */
export function openWorkGroup(group: WorkGroupId, itemId?: string) {
  const ui = useUI.getState();
  const prior = ui.surfaceStates.work;
  ui.setSurfaceState("work", {
    ...prior,
    section: workSectionForGroup(group),
    itemId,
    scrollTop: 0,
    filters: prior?.filters,
  });
  if (group === "needs-you") endThisChatVisit();
  else beginThisChatVisit();
  openView("work");
}

/** Keep Tab inside the details panel (the DS Drawer traps on its own; the Radix popover
 *  doesn't). Wraps forward from the last focusable and backward from the first. */
export function trapTab(event: ReactKeyboardEvent<HTMLElement>) {
  if (event.key !== "Tab") return;
  const panel = event.currentTarget;
  const focusables = [...panel.querySelectorAll<HTMLElement>(
    "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
  )];
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || active === panel)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

/** The chip's leading glyph. The goal stays visible inside the Doing now chip; with no goal
 *  it uses the spinner. DS Spinner/StatusDot carry their own status semantics — wrapped
 *  aria-hidden so the rail keeps exactly ONE live region (§6). */
function chipIcon(chip: RailChip): ReactNode {
  if (chip.problem) return <span aria-hidden="true"><StatusDot status="warning" /></span>;
  switch (chip.id) {
    case "doing-now":
      return chip.goal ? <Target size={12} aria-hidden /> : <span aria-hidden="true"><Spinner size={12} /></span>;
    case "coming-up":
      return <Clock size={12} aria-hidden />;
    case "instructions":
      return <span aria-hidden="true"><StatusDot status="info" /></span>;
  }
}

function GroupSection({ group, directives, mobile, onNavigate }: {
  group: WorkGroup;
  /** THIS chat's instructions source, for its preserved error / stale "Load again" states. */
  directives: WorkSource | null;
  mobile: boolean;
  onNavigate: () => void;
}) {
  const failed = directives?.status === "error";
  const stale = directives?.status === "stale";
  if (!group.count && !failed) return null;
  // THIS chat's ledger never loaded: the count is unknowable, so the heading carries the
  // name alone — never a claimed "0" (the chip hides its count the same way).
  const unknownCount = failed && group.count === 0;
  const name = unknownCount ? group.label : `${group.label}, ${group.count}`;
  const size = mobile ? "md" : "sm";
  return (
    <section className="chat-status-details__section" aria-label={name}>
      <h2 className="chat-status-details__heading" aria-label={name}>
        {group.label}
        {!unknownCount && <span aria-hidden="true"> · {group.count}</span>}
      </h2>
      {failed ? (
        <p className="chat-status-details__problem">Couldn&apos;t check your instructions.</p>
      ) : (
        <ul className="chat-status-details__rows">
          {group.items.map((item) => (
            <li key={`${item.kind}:${item.id}`} className="chat-status-details__row">
              <span className="chat-status-details__title" title={item.title}>{item.title}</span>
              {item.meta ? <span className="chat-status-details__meta">{item.meta}</span> : null}
            </li>
          ))}
        </ul>
      )}
      {stale && <p className="chat-status-details__problem">Your instructions may be out of date.</p>}
      <div className="chat-status-details__actions">
        {(failed || stale) && (
          <Button size={size} variant="ghost" onClick={() => directives?.retry()}>Load again</Button>
        )}
        <Button
          size={size}
          variant="ghost"
          onClick={() => {
            onNavigate();
            openWorkGroup(group.id);
          }}
        >
          Open in Work
        </Button>
      </div>
    </section>
  );
}

/** The picker (§ the v7 people behavior): EVERY cast member as a toggle chip, the dynamic
 *  recipients line, and the "In this chat · N" heading. One component serves both hosts —
 *  the `+K` chip's own panel, and the People section the grouped details gain while names
 *  are hidden. Toggling edits only the draft's leading run (the message body is untouched)
 *  and never closes the panel, so several people can be picked in one visit; focus stays on
 *  the clicked chip even though the toggle reorders the rail behind the panel. */
function PeopleSection({ cast, draft, onToggleMention }: {
  cast: string[];
  draft: string;
  onToggleMention: (name: string, source?: "picker") => void;
}) {
  const line = recipientLine(draft, cast);
  const name = `In this chat, ${cast.length}`;
  return (
    <section className="chat-status-details__section chat-people" aria-label={name}>
      <h2 className="chat-status-details__heading" aria-label={name}>
        In this chat<span aria-hidden="true"> · {cast.length}</span>
      </h2>
      <div className="chat-people__chips">
        {cast.map((person) => {
          const active = runHas(draft, person);
          return (
            <button
              key={person}
              type="button"
              className={`chat-roster-chip${active ? " chat-roster-chip--active" : ""}`}
              aria-pressed={active}
              title={active ? `Remove @${person} from this message` : `Address @${person}`}
              onClick={(event) => {
                onToggleMention(person, "picker");
                // The caller may move focus for a RAIL toggle (to the composer); a picker
                // toggle keeps it here so the next pick is one Tab away.
                event.currentTarget.focus();
              }}
            >
              <span className="chat-roster-name">@{person}</span>
            </button>
          );
        })}
      </div>
      {line && <p className="chat-people__line">{line}</p>}
    </section>
  );
}

function DetailsPanel({ groups, directives, mobile, onNavigate, people, needsYou }: {
  groups: WorkGroup[];
  /** THIS chat's instructions source — status and retry scoped to its own ledger. */
  directives: WorkSource;
  mobile: boolean;
  onNavigate: () => void;
  /** The People section — present ONLY while the rail hides names behind `+K`. */
  people?: ReactNode;
  /** The Needs-you section (S7) — the FIRST section whenever something needs the operator. */
  needsYou?: ReactNode;
}) {
  return (
    <div className="chat-status-details" onKeyDown={trapTab}>
      {needsYou}
      {groups.filter((group) => group.id !== "needs-you").map((group) => (
        <GroupSection
          key={group.id}
          group={group}
          directives={group.id === "instructions" ? directives : null}
          mobile={mobile}
          onNavigate={onNavigate}
        />
      ))}
      {people}
    </div>
  );
}

export function ChatStatusRail({ sessionId, cast, draft, onToggleMention }: {
  sessionId: string;
  /** Who is in this chat — the transcript-derived cast (#3049), chips unchanged. */
  cast: string[];
  /** The live draft: a cast chip is active while its mention leads the draft's run. */
  draft: string;
  /** Toggle `name` in the draft's leading run. `source === "picker"` tells the caller the
   *  toggle came from the open picker, where focus must NOT jump to the composer. */
  onToggleMention: (name: string, source?: "picker") => void;
}) {
  const mobile = useIsMobile();
  const sources = useWorkItems({ sessionId, thisChatOnly: false });
  // The shared decisions store (useDecisions) overlays the Work rows exactly like the Work
  // view does, so an answer drops the marker's count the moment it lands — the two surfaces
  // read ONE projection and cannot drift. No second answer cache: the store owns the records.
  const decisionsShared = useDecisions();
  const merged = useMemo(
    () => withSharedDecisions(sources, { records: decisionsShared.records, missing: decisionsShared.missing }),
    [sources, decisionsShared.records, decisionsShared.missing],
  );
  const flat = useMemo(() => flattenWorkItems(merged), [merged]);
  const groups = useMemo(() => workGroups(flat, { thisChatOnly: true, sessionId }), [flat, sessionId]);
  const chips = railChips(groups);
  // Fleet-wide Needs you (S7): the selector never chat-scopes this group, so the marker's
  // count is every pending decision, undelivered human-needed page, stopped goal and
  // `waiting` task (ADR 0129 Q1) anywhere — EXACTLY the kinds the section draws
  // (NEEDS_YOU_SECTION_KINDS), so the marker and Work's Needs-you count can't drift.
  const needsYou = useMemo(() => {
    const group = groups.find((candidate) => candidate.id === "needs-you")!;
    const items = group.items.filter((item) => NEEDS_YOU_SECTION_KINDS.has(item.kind));
    return { ...group, items, count: items.length };
  }, [groups]);
  // Interacted decision cards the OPEN panel retains (§5.5 hold): a result stays readable
  // until the panel closes, while the marker itself counts pending rows only.
  const [held, setHeld] = useState<Map<string, DecisionRow>>(() => new Map());
  const holdDecision = (row: DecisionRow) => setHeld((prev) => new Map(prev).set(row.id, row));
  // THIS chat's ledger, from the same per-session store the old DirectiveStrip read. The
  // aggregated `sources.directives` sums every chat's ledgers — its `loadedAt` is the max
  // across sessions, so a failure HERE reads as merely "stale" there the moment any other
  // chat has ever loaded, and another chat's (or the session index's) failure would wrongly
  // mark this chat's instructions out of date. The rail counts this-chat-only; its preserved
  // failure states are scoped the same way, and Load again refreshes this one ledger.
  const ledger = useDirectives(sessionId);
  const ledgerStatus: WorkSource["status"] =
    ledger.state === "failed" ? "error"
      : ledger.state === "stale" ? "stale"
        : ledger.state === "loading" ? "loading" : "ready";
  const instructionsSource: WorkSource = {
    ...sources.directives,
    status: ledgerStatus,
    loadedAt: ledger.loadedAt,
    error: ledgerStatus === "error" || ledgerStatus === "stale"
      ? new Error("This chat's directive ledger could not be refreshed") : null,
    retry: () => { void ledger.refresh(); },
  };
  // The failed-instructions chip: THIS chat's ledger never loaded, so the count is unknowable —
  // it renders without one and the details panel carries the preserved "Load again" path.
  if (ledgerStatus === "error" && !chips.some((chip) => chip.id === "instructions")) {
    chips.push({ id: "instructions", label: "Told it", count: 0, problem: true });
  }

  // ── People fit (S6b, the v7 behavior): one row, measured, `+K` for the rest ─────────
  // Draft-addressed names first, then the original cast order. Run order alone is not
  // enough for the card's "newly picked name takes a visible slot": toggleMention appends
  // the pick to the run's END, so with the visible slots already full of addressed names
  // the pick would stay behind `+K`. Track the picker's latest add and orderPeople leads
  // the addressed group with it. A rail-chip add never needs that: the chip it lands on is
  // visible already and joining the addressed group cannot push it out of the window. A
  // removal of the promoted name (either host) ends its promotion; so does anything that
  // drops it from the run (orderPeople ignores a pick the run no longer addresses).
  const [latestPick, setLatestPick] = useState<string | null>(null);
  const toggleMentionTracked = (name: string, source?: "picker") => {
    if (runHas(draft, name)) {
      setLatestPick((prev) => (prev && prev.toLowerCase() === name.toLowerCase() ? null : prev));
    } else if (source === "picker") {
      setLatestPick(name);
    }
    // Forward the exact call shape the hosts always made (the rail passes no source).
    if (source) onToggleMention(name, source);
    else onToggleMention(name);
  };
  const ordered = useMemo(() => orderPeople(cast, draft, latestPick), [cast, draft, latestPick]);
  const rosterRef = useRef<HTMLDivElement | null>(null);
  const measureRef = useRef<HTMLDivElement | null>(null);
  // The roster's content width (what the group chips leave on the row), from ResizeObserver —
  // null until the first delivery, which is when the fallback caps apply.
  const [available, setAvailable] = useState<number | null>(null);
  const [measured, setMeasured] = useState<number | null>(null);
  const hasRoster = cast.length > 0;
  useEffect(() => {
    const node = rosterRef.current;
    if (!hasRoster || !node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[entries.length - 1]?.contentRect.width;
      if (typeof width === "number") setAvailable(Math.floor(width));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasRoster]);
  // Measure the hidden mirror row (same classes, same gaps, every chip plus the widest
  // possible `+K`) before paint: a chip's offsetLeft already carries the icon and gaps, so
  // the pure fit arithmetic needs no layout knowledge of its own. Reruns when the width,
  // the people, or their order change; jsdom and the pre-observer first paint keep
  // `measured` null and ride the fallback caps instead.
  const orderedKey = ordered.join("\u0000");
  useLayoutEffect(() => {
    const row = measureRef.current;
    if (available == null || !row) {
      setMeasured(null);
      return;
    }
    const children = [...row.children] as HTMLElement[];
    const more = children[children.length - 1];
    const boxes = children.slice(1, -1).map((el) => ({ left: el.offsetLeft, width: el.offsetWidth }));
    setMeasured(fitCount(boxes, more.offsetWidth, available));
  }, [available, orderedKey]);
  const visibleCount = Math.min(measured ?? fallbackCap(mobile, chips.length > 0, needsYou.count > 0), ordered.length);
  const visible = ordered.slice(0, visibleCount);
  const hiddenCount = ordered.length - visibleCount;

  type OpenId = RailChip["id"] | "people" | "needs-you" | null;
  const [open, setOpen] = useState<OpenId>(null);
  const chipRefs = useRef(new Map<Exclude<OpenId, "people" | null>, HTMLButtonElement>());
  const moreRef = useRef<HTMLButtonElement | null>(null);
  const lastOpen = useRef<OpenId>(null);
  if (open !== null) lastOpen.current = open;
  // The open chip can leave the rail under the reader (its last job ends, its count hits
  // zero; for the picker, a resize lets everyone fit and `+K` goes). Clear the state too,
  // or the panel would reopen by itself when the chip returns. The Needs-you marker with
  // results HELD stays open at zero — the hold is the §5.5 contract — until the panel closes.
  const openGone = open !== null
    && (open === "people" ? hiddenCount === 0
      : open === "needs-you" ? needsYou.count === 0 && held.size === 0
        : !chips.some((chip) => chip.id === open));
  useEffect(() => {
    if (openGone) setOpen(null);
  }, [openGone]);
  // The hold lives exactly as long as a panel is open: closing any panel releases it.
  useEffect(() => {
    if (open === null) setHeld((prev) => (prev.size ? new Map() : prev));
  }, [open]);

  // One polite, change-only live region (§6): empty on mount, written ONLY when a job with
  // tied approvals ends — "<job> finished. 1 approval it carried is now fulfilled." (§3). No
  // clock chatter, and no claim the server fulfilled anything (that lands with ADR S3).
  const [announcement, setAnnouncement] = useState("");
  const seen = useRef<{ ended: Set<string>; tied: Map<string, number> } | null>(null);
  useEffect(() => {
    const tied = new Map<string, number>();
    for (const item of flat) {
      if (item.kind !== "directive" || !item.tiedJob) continue;
      if ((item.raw as { status?: string } | null)?.status !== "in_force") continue;
      if (!inWorkScope(item, { thisChatOnly: true, sessionId })) continue;
      const key = jobKey(item.tiedJob);
      tied.set(key, (tied.get(key) ?? 0) + 1);
    }
    const ended = new Map<string, string>();
    for (const item of flat) {
      if ((item.kind === "task" || item.kind === "background" || item.kind === "goal") && jobEnded(item)) {
        ended.set(jobKey({ kind: item.kind, id: item.id }), item.title);
      }
    }
    const prev = seen.current;
    if (prev) {
      // Every tied job that ended in THIS update speaks — one joined write, so two jobs
      // finishing in the same refetch cannot overwrite each other's completion.
      const finished: string[] = [];
      for (const [key, title] of ended) {
        if (prev.ended.has(key)) continue;
        const carried = tied.get(key) ?? prev.tied.get(key) ?? 0;
        if (!carried) continue;
        finished.push(
          `${title} finished. ${carried} approval${carried === 1 ? "" : "s"} it carried ` +
            `${carried === 1 ? "is" : "are"} now fulfilled.`,
        );
      }
      if (finished.length) setAnnouncement(finished.join(" "));
    }
    seen.current = { ended: new Set(ended.keys()), tied };
  }, [flat, sessionId]);

  const close = () => setOpen(null);
  // Return focus to the opener. The `+K` button is one keyed node, so it survives the
  // reorders a picker visit causes — focus lands back on it wherever it moved to.
  const closeAndRestore = () => {
    setOpen(null);
    if (lastOpen.current === "people") moreRef.current?.focus();
    else if (lastOpen.current) chipRefs.current.get(lastOpen.current)?.focus();
  };

  const hasContent = chips.length > 0 || cast.length > 0 || needsYou.count > 0
    || (open === "needs-you" && held.size > 0);
  // The ONE polite live region (§6) — mounted for the component's whole life, OUTSIDE the
  // conditional rail, at a fixed child position so React never recreates the node. A screen
  // reader announces text that CHANGES inside a region that already existed; a region that
  // (re)mounts already holding its text is read by nothing. The old shape returned null for
  // the render in which a tied-job completion emptied the rail (the announcement lands in the
  // effect afterwards), remounting the region WITH its text — the announcement was lost. The
  // span is the sr-only class (position: absolute), so an empty rail still takes no space.
  const liveRegion = (
    <span className="chat-status-rail__announce" role="status" aria-live="polite" aria-atomic="true">
      {announcement}
    </span>
  );

  const peopleSection = (
    <PeopleSection cast={cast} draft={draft} onToggleMention={toggleMentionTracked} />
  );
  // The grouped details gain the People section ONLY while the rail hides names — with
  // everyone visible on the row there is nothing the panel would add.
  const peopleWhenHidden = hiddenCount > 0 ? peopleSection : undefined;

  // `+K` — the overflow chip. Visible text is the count alone; the spoken label says it
  // all. One keyed node, so focus return still works after a picker visit reorders the row.
  const moreChip = (
    <button
      type="button"
      className="chat-roster-chip chat-roster-more"
      data-testid="chat-roster-more"
      ref={moreRef}
      aria-haspopup="dialog"
      aria-expanded={open === "people"}
      aria-label={spokenMoreLabel(hiddenCount)}
      onClick={mobile ? () => setOpen(open === "people" ? null : "people") : undefined}
    >
      +{hiddenCount}
    </button>
  );

  // The Needs-you section — the panel's FIRST section wherever the panel opens from. Present
  // while anything is pending, and while an interacted result is held in the open panel.
  const needsYouSectionFor = (onNavigate: () => void) =>
    needsYou.count > 0 || held.size > 0 ? (
      <NeedsYouSection
        items={needsYou.items}
        held={held}
        onHold={holdDecision}
        records={decisionsShared.records}
        answer={decisionsShared.answer}
        mobile={mobile}
        onNavigate={onNavigate}
        openWork={(itemId) => openWorkGroup("needs-you", itemId)}
        goalsRetry={() => sources.goals.retry()}
      />
    ) : undefined;

  // The S7 marker: FIRST on the row, primary DS Button (xs), fleet-wide count, hidden at
  // zero, never folded. The phone hides the wordy span (numbers only); the spoken label says
  // it all. It survives a count of zero ONLY while its open panel holds a result (§5.5).
  const needsYouExpanded = open === "needs-you";
  const needsYouButton = (
    <RefButton
      size="xs"
      variant="primary"
      className="chat-status-needs-you"
      data-testid="chat-status-chip-needs-you"
      ref={(node: HTMLButtonElement | null) => {
        if (node) chipRefs.current.set("needs-you", node);
        else chipRefs.current.delete("needs-you");
      }}
      aria-haspopup="dialog"
      aria-expanded={needsYouExpanded}
      aria-label={needsYouSpokenLabel(needsYou.count)}
      onClick={mobile ? () => setOpen(needsYouExpanded ? null : "needs-you") : undefined}
    >
      <span className="chat-status-chip__text">Needs you</span>
      {needsYou.count > 0 && <span className="chat-status-chip__count">{needsYou.count}</span>}
    </RefButton>
  );
  const needsYouMarker = needsYou.count === 0 && !(needsYouExpanded && held.size > 0)
    ? null
    : mobile ? needsYouButton : (
      <Popover
        side="top"
        align="start"
        className="chat-status-popover"
        open={needsYouExpanded}
        onOpenChange={(next) => {
          setOpen(next ? "needs-you" : null);
          if (!next) chipRefs.current.get("needs-you")?.focus();
        }}
        trigger={needsYouButton}
      >
        <DetailsPanel
          groups={groups}
          directives={instructionsSource}
          mobile={false}
          onNavigate={close}
          people={peopleWhenHidden}
          needsYou={needsYouSectionFor(close)}
        />
      </Popover>
    );

  const renderChip = (chip: RailChip) => {
    const expanded = open === chip.id;
    const button = (
      <button
        type="button"
        className={`chat-status-chip${chip.problem ? " chat-status-chip--problem" : ""}`}
        data-testid={`chat-status-chip-${chip.id}`}
        ref={(node) => {
          if (node) chipRefs.current.set(chip.id, node);
          else chipRefs.current.delete(chip.id);
        }}
        aria-haspopup="dialog"
        aria-expanded={expanded}
        aria-label={spokenLabel(chip)}
        onClick={mobile ? () => setOpen(expanded ? null : chip.id) : undefined}
      >
        {chipIcon(chip)}
        <span className="chat-status-chip__text">
          {chip.label}
          {chip.goal ? ` · goal ${chip.goal.step}/${chip.goal.of ?? "∞"} · ` : chip.problem ? "" : " "}
        </span>
        {!chip.problem && <span className="chat-status-chip__count">{chip.count}</span>}
      </button>
    );
    if (mobile) return <Fragment key={chip.id}>{button}</Fragment>;
    return (
      <Popover
        key={chip.id}
        side="top"
        align="start"
        className="chat-status-popover"
        open={expanded}
        onOpenChange={(next) => {
          setOpen(next ? chip.id : null);
          // Radix returns focus to its trigger on most closes; do it explicitly so the
          // return-focus contract holds on every path (and deterministically in jsdom).
          if (!next) chipRefs.current.get(chip.id)?.focus();
        }}
        trigger={button}
      >
        <DetailsPanel
          groups={groups}
          directives={instructionsSource}
          mobile={false}
          onNavigate={close}
          people={peopleWhenHidden}
          needsYou={needsYouSectionFor(close)}
        />
      </Popover>
    );
  };

  return (
    <>
      {liveRegion}
      {hasContent && (
        <div className="chat-status-rail" role="group" aria-label="Chat status" data-testid="chat-status-rail">
          {needsYouMarker}
          {chips.map(renderChip)}
          {cast.length ? (
            <div className="chat-roster" aria-label="In this chat" ref={rosterRef}>
              <Users size={13} aria-hidden />
              {visible.map((name) => {
                const active = runHas(draft, name);
                return (
                  <button
                    key={name}
                    type="button"
                    className={`chat-roster-chip${active ? " chat-roster-chip--active" : ""}`}
                    aria-pressed={active}
                    title={active ? `Remove @${name} from this message` : `Address @${name}`}
                    onClick={() => toggleMentionTracked(name)}
                  >
                    <span className="chat-roster-name">@{name}</span>
                  </button>
                );
              })}
              {hiddenCount > 0 && (mobile ? moreChip : (
                <Popover
                  side="top"
                  align="end"
                  className="chat-status-popover"
                  open={open === "people"}
                  onOpenChange={(next) => {
                    setOpen(next ? "people" : null);
                    if (!next) moreRef.current?.focus();
                  }}
                  trigger={moreChip}
                >
                  <div className="chat-status-details" onKeyDown={trapTab}>{peopleSection}</div>
                </Popover>
              ))}
              {/* The hidden mirror of the whole people row — the icon, EVERY chip in display
                  order, then the widest `+K` this cast could need — which the layout effect
                  measures. Same classes as the live chips, so it can never drift from the
                  real metrics; spans, not buttons, so it adds no tab stops. */}
              <div className="chat-roster__measure" aria-hidden="true" ref={measureRef}>
                <Users size={13} aria-hidden />
                {ordered.map((name) => (
                  <span key={name} className="chat-roster-chip">
                    <span className="chat-roster-name">@{name}</span>
                  </span>
                ))}
                <span className="chat-roster-chip">+{Math.max(ordered.length - 1, 1)}</span>
              </div>
            </div>
          ) : null}
          {mobile && (
            <Drawer
              open={open !== null}
              onClose={closeAndRestore}
              side="bottom"
              title="Chat status"
              className="chat-status-sheet"
            >
              {open === "people" ? (
                // The picker sheet opens AT "In this chat · N" — the People section alone.
                <div className="chat-status-details" onKeyDown={trapTab}>{peopleSection}</div>
              ) : open !== null ? (
                <DetailsPanel
                  groups={groups}
                  directives={instructionsSource}
                  mobile
                  onNavigate={closeAndRestore}
                  people={peopleWhenHidden}
                  needsYou={needsYouSectionFor(closeAndRestore)}
                />
              ) : null}
            </Drawer>
          )}
        </div>
      )}
    </>
  );
}
