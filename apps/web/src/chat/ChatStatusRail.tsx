/** The chat status rail (work consolidation v2 §5, S6a) — the shared-count strip above the
 *  composer. One chip per non-empty Work group (Doing now · Coming up · Told it), counted by
 *  the SAME `workGroups` selector the Work view reads, with `thisChat` applied, so the chip
 *  numbers and the Work group rows cannot drift. Each chip opens one grouped details panel —
 *  a DS Popover on desktop, a DS bottom-sheet Drawer on the phone — with one section per Work
 *  group (Work's own names) and an "Open in Work" deep link. The people (cast) chips keep
 *  their mention-toggle behavior unchanged, after the group chips.
 *
 *  Needs you is S7-gated: the selector computes it, the rail doesn't render it yet. */
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { Button } from "@protolabsai/ui/primitives";
import { Spinner, StatusDot } from "@protolabsai/ui/data";
import { Drawer, Popover } from "@protolabsai/ui/overlays";
import { Clock, Target, Users } from "lucide-react";

import { openView } from "../app/palette/nav";
import { useUI } from "../state/uiStore";
import { useIsMobile } from "../lib/useIsMobile";
import type { GoalState } from "../lib/types";
import { sections } from "../work/sectionRegistry";
import { useWorkItems, type WorkSource } from "../work/useWorkItems";
import { flattenWorkItems, inWorkScope, jobEnded, jobKey, workGroups, type WorkGroup } from "../work/workGroups";
import type { WorkGroupId } from "../work/tone";
import { useDirectives } from "./useDirectives";
import { runHas } from "./mentionRun";
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

/** The §5 chips from the shared selector's groups: Needs you is S7-gated, zero counts hide. */
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

/** Where "Open in Work" lands for a group. The four-group Work sections are their own slice:
 *  once a section registers under the group id itself, the deep link targets it directly.
 *  Until then, Your instructions maps to its registered section ("directives", which already
 *  carries the v2 label) and the running/planned groups land on the Overview, whose lanes
 *  show those same rows today. */
export function workSectionForGroup(group: WorkGroupId): string {
  if (sections.has(group)) return group;
  return group === "instructions" ? "directives" : group === "needs-you" ? "decisions" : "overview";
}

/** Deep link to a Work group under the `thisChat` scope the chip counted with.
 *  (Needs you, when S7 adds it, deliberately won't set the filter — it is never scoped.) */
export function openWorkGroup(group: WorkGroupId) {
  const ui = useUI.getState();
  const prior = ui.surfaceStates.work;
  ui.setSurfaceState("work", {
    ...prior,
    section: workSectionForGroup(group),
    itemId: undefined,
    scrollTop: 0,
    filters: group === "needs-you" ? prior?.filters : { ...prior?.filters, thisChat: true },
  });
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
  const size = mobile ? "md" : "sm";
  return (
    <section className="chat-status-details__section" aria-label={`${group.label}, ${group.count}`}>
      <h2 className="chat-status-details__heading" aria-label={`${group.label}, ${group.count}`}>
        {group.label}
        <span aria-hidden="true"> · {group.count}</span>
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

function DetailsPanel({ groups, directives, mobile, onNavigate }: {
  groups: WorkGroup[];
  /** THIS chat's instructions source — status and retry scoped to its own ledger. */
  directives: WorkSource;
  mobile: boolean;
  onNavigate: () => void;
}) {
  return (
    <div className="chat-status-details" onKeyDown={trapTab}>
      {groups.filter((group) => group.id !== "needs-you").map((group) => (
        <GroupSection
          key={group.id}
          group={group}
          directives={group.id === "instructions" ? directives : null}
          mobile={mobile}
          onNavigate={onNavigate}
        />
      ))}
    </div>
  );
}

export function ChatStatusRail({ sessionId, cast, draft, onToggleMention }: {
  sessionId: string;
  /** Who is in this chat — the transcript-derived cast (#3049), chips unchanged. */
  cast: string[];
  /** The live draft: a cast chip is active while its mention leads the draft's run. */
  draft: string;
  onToggleMention: (name: string) => void;
}) {
  const mobile = useIsMobile();
  const sources = useWorkItems({ sessionId, thisChatOnly: false });
  const flat = useMemo(() => flattenWorkItems(sources), [sources]);
  const groups = useMemo(() => workGroups(flat, { thisChatOnly: true, sessionId }), [flat, sessionId]);
  const chips = railChips(groups);
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

  const [open, setOpen] = useState<RailChip["id"] | null>(null);
  const chipRefs = useRef(new Map<RailChip["id"], HTMLButtonElement>());
  const lastOpen = useRef<RailChip["id"] | null>(null);
  if (open !== null) lastOpen.current = open;
  // The open chip can leave the rail under the reader (its last job ends, its count hits
  // zero). Clear the state too, or the panel would reopen by itself when the chip returns.
  const openGone = open !== null && !chips.some((chip) => chip.id === open);
  useEffect(() => {
    if (openGone) setOpen(null);
  }, [openGone]);

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
  const closeAndRestore = () => {
    setOpen(null);
    if (lastOpen.current) chipRefs.current.get(lastOpen.current)?.focus();
  };

  const hasContent = chips.length > 0 || cast.length > 0;
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
        <DetailsPanel groups={groups} directives={instructionsSource} mobile={false} onNavigate={close} />
      </Popover>
    );
  };

  return (
    <>
      {liveRegion}
      {hasContent && (
        <div className="chat-status-rail" role="group" aria-label="Chat status" data-testid="chat-status-rail">
          {chips.map(renderChip)}
          {cast.length ? (
            <div className="chat-roster" aria-label="In this chat">
              <Users size={13} aria-hidden />
              {cast.map((name) => {
                const active = runHas(draft, name);
                return (
                  <button
                    key={name}
                    type="button"
                    className={`chat-roster-chip${active ? " chat-roster-chip--active" : ""}`}
                    aria-pressed={active}
                    title={active ? `Remove @${name} from this message` : `Address @${name}`}
                    onClick={() => onToggleMention(name)}
                  >
                    <span className="chat-roster-name">@{name}</span>
                  </button>
                );
              })}
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
              {open !== null && (
                <DetailsPanel groups={groups} directives={instructionsSource} mobile onNavigate={closeAndRestore} />
              )}
            </Drawer>
          )}
        </div>
      )}
    </>
  );
}
