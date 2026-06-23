import "./tool-calls.css";
import {
  Calculator,
  Clock,
  Database,
  Globe,
  Network,
  Search,
  Square,
  Wrench,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { ToolCard, ToolCardList, ToolCardSummary, ToolSection } from "@protolabsai/ui/tool-card";

import type { ToolCall } from "../lib/types";
import { ToolValue } from "./tool-renderers";

/** Map a tool name to a recognizable icon; falls back to a generic wrench. */
function iconFor(name: string): LucideIcon {
  if (name === "calculator") return Calculator;
  if (name === "web_search") return Search;
  if (name === "fetch_url") return Globe;
  if (name === "current_time") return Clock;
  if (name === "task") return Network; // subagent delegation
  if (name.startsWith("memory")) return Database;
  return Wrench;
}

/** Header label for a card. A `task` delegation surfaces WHICH subagent it ran
 *  (`task → researcher`), read from the call's args, so the roster is visible at a
 *  glance without expanding. The subagent type rides in `input` from the start frame,
 *  so it shows while the delegation is still running; falls back to the bare name until
 *  the args parse. */
function cardLabel(call: ToolCall): ReactNode {
  if (call.name !== "task" || !call.input) return call.name;
  try {
    const args = JSON.parse(call.input) as { subagent_type?: unknown };
    const sub = args.subagent_type;
    if (typeof sub === "string" && sub) {
      return (
        <>
          task <span className="tool-subagent">→ {sub}</span>
        </>
      );
    }
  } catch {
    /* args not valid JSON yet (mid-stream) — fall back to the bare name */
  }
  return call.name;
}

/**
 * Renders the agent's tool activity as collapsible cards inside an assistant
 * message. Each card shows the tool name, a running→done/error state pill, and
 * (when expanded) the input preview + result preview the server streamed over
 * the tool-call DataPart. The disclosure FRAME (card chrome, header, caret,
 * status glyph, duration, nesting) is the DS `ToolCard` family; the body is our
 * per-tool value renderers (`ToolValue` via `tool-renderers.tsx`).
 */
export function ToolCalls({
  calls,
  streaming = false,
  flat = false,
  spotlight = false,
  onCancelDelegation,
}: {
  calls: ToolCall[];
  /** The turn is still live. Keeps the spotlight slot reserved for the whole turn so the
   *  layout doesn't bounce in the gap between one tool finishing and the next starting. */
  streaming?: boolean;
  /** Render every card plainly — no spotlight, no fold chip. For use INSIDE the WorkBlock,
   *  where the whole reason→tool timeline is already folded behind one disclosure. */
  flat?: boolean;
  /** Spotlight ONLY the most-recent tool, in a single slot with a STABLE identity — the
   *  card updates in place (name/status/output swap) as tools advance instead of remounting
   *  per tool. Without this, a rapid fan-out (e.g. task_batch's many children) strobes: each
   *  new id remounts the card, replaying its mount animation and flashing the prior output. */
  spotlight?: boolean;
  /** Abort a running top-level `task` delegation by its tool-call id (Tier 2). When
   *  omitted, no Stop affordance renders (e.g. historical/finished messages). */
  onCancelDelegation?: (id: string) => void;
}) {
  // Group children (tools that ran inside a `task` subagent) under their parent.
  const childrenByParent = new Map<string, ToolCall[]>();
  const top: ToolCall[] = [];
  for (const call of calls) {
    if (call.parentId) {
      const arr = childrenByParent.get(call.parentId);
      if (arr) arr.push(call);
      else childrenByParent.set(call.parentId, [call]);
    } else {
      top.push(call);
    }
  }
  const settled = top.filter((c) => c.status !== "running");
  const failedCount = top.filter((c) => c.status === "error").length;

  // Only TOP-LEVEL `task` groups get the cancel callback (the Stop affordance only shows
  // for a running task); nested children and settled cards never need it.
  const group = (call: ToolCall) => (
    <ToolGroup
      key={call.id}
      call={call}
      childrenByParent={childrenByParent}
      onCancelDelegation={call.status === "running" ? onCancelDelegation : undefined}
    />
  );

  // The folded summary chip — the block's running total, with the given finished cards inside.
  const chip = (count: number, folded: ToolCall[]) => (
    <ToolCardSummary
      count={count}
      label={count === 1 ? "tool" : "tools"}
      status={failedCount > 0 ? "error" : "done"}
      failedCount={failedCount || undefined}
    >
      {folded.map(group)}
    </ToolCardSummary>
  );

  // Inside the WorkBlock: just the plain cards, in order (the timeline is already folded).
  if (flat) {
    return <ToolCardList className="tool-calls">{top.map(group)}</ToolCardList>;
  }

  // A single, identity-STABLE slot holding only the most-recent tool. The fixed key keeps
  // React updating one card in place as the current tool changes, so a fast fan-out advances
  // smoothly instead of remounting (and strobing) on every new tool id.
  if (spotlight) {
    if (top.length === 0) return null;
    const current = top[top.length - 1];
    return (
      <ToolCardList className="tool-calls">
        <div className="tool-spotlight">
          <ToolGroup
            key="__spotlight__"
            call={current}
            childrenByParent={childrenByParent}
            onCancelDelegation={current.status === "running" ? onCancelDelegation : undefined}
          />
        </div>
      </ToolCardList>
    );
  }

  // LIVE TURN: keep the MOST-RECENT tool in the spotlight slot until a newer one replaces
  // it — so the slot is never empty (no blank gap between tools, or during the answer tail
  // after the last tool finishes). Everything older folds into the running-total chip; a
  // new tool crossfades into the slot and the previous one drops into the chip.
  if (streaming) {
    if (top.length === 0) return null;
    const current = top[top.length - 1];
    const folded = top.slice(0, -1);
    return (
      <ToolCardList className="tool-calls">
        {/* Stable key: the slot updates in place as the current tool advances (no remount
            strobe — see the `spotlight` prop note). */}
        <div className="tool-spotlight">
          <ToolGroup
            key="__spotlight__"
            call={current}
            childrenByParent={childrenByParent}
            onCancelDelegation={current.status === "running" ? onCancelDelegation : undefined}
          />
        </div>
        {folded.length > 0 && chip(top.length, folded)}
      </ToolCardList>
    );
  }

  // SETTLED (turn done for this block): a lone finished tool renders inline (no pointless
  // "1 tool" chip); a real fan-out (≥2) stays folded.
  if (settled.length >= 2) {
    return <ToolCardList className="tool-calls">{chip(settled.length, settled)}</ToolCardList>;
  }
  return <ToolCardList className="tool-calls">{top.map(group)}</ToolCardList>;
}

/** A tool card. For a subagent `task`, its child tool cards collapse INSIDE the card's
 *  body (revealed on expand) and the header shows a running count
 *  ("task → researcher · 3 tools"). Keeping them in the collapsible body — not the DS
 *  always-on `nested` rail — is what lets the card hold a STABLE one-row height while the
 *  subagent works, instead of growing a rail and then collapsing when it folds. */
function ToolGroup({
  call,
  childrenByParent,
  onCancelDelegation,
}: {
  call: ToolCall;
  childrenByParent: Map<string, ToolCall[]>;
  onCancelDelegation?: (id: string) => void;
}) {
  const kids = childrenByParent.get(call.id);
  const nestedCards = kids?.length
    ? kids.map((kid) => (
        // Children inherit no cancel callback — they aren't independent delegations.
        <ToolGroup key={kid.id} call={kid} childrenByParent={childrenByParent} />
      ))
    : undefined;

  // Collapsed by default; expanding reveals the args/result AND the subagent's nested
  // tools (the `.pl-toolcard__children` indented rail, but here gated by the card's open
  // state instead of always-on — so the header row stays a stable height as kids stream in).
  const Icon = iconFor(call.name);
  const body =
    call.input || call.output || nestedCards ? (
      <>
        {call.input ? (
          <ToolSection label="input" copyText={call.input}>
            <ToolValue raw={call.input} role="input" tool={call.name} />
          </ToolSection>
        ) : null}
        {call.output ? (
          <ToolSection label="result" copyText={call.output}>
            <ToolValue raw={call.output} role="output" tool={call.name} />
          </ToolSection>
        ) : null}
        {nestedCards ? <div className="pl-toolcard__children">{nestedCards}</div> : null}
      </>
    ) : undefined;

  // A running subagent delegation can be aborted (Tier 2): Stop cancels just this
  // `task`, the lead keeps working. Lives in the DS ToolCard header `actions` slot
  // (a sibling of the disclosure toggle, so it doesn't expand the card).
  const actions =
    onCancelDelegation && call.name === "task" && call.status === "running" ? (
      <button
        type="button"
        className="pl-iconbtn tool-cancel-btn"
        title="Stop this delegation — the agent keeps working without its result"
        aria-label="Stop this delegation"
        onClick={(e) => {
          e.stopPropagation();
          onCancelDelegation(call.id);
        }}
      >
        <Square size={11} />
        <span>Stop</span>
      </button>
    ) : undefined;

  // A running count of the subagent's tools, in the header — so a collapsed delegation
  // reads "task → researcher · 3 tools" at a glance without expanding.
  const kidCount = kids?.length ?? 0;
  const name =
    kidCount > 0 ? (
      <>
        {cardLabel(call)}
        <span className="tool-nested-count">
          {" · "}
          {kidCount} {kidCount === 1 ? "tool" : "tools"}
        </span>
      </>
    ) : (
      cardLabel(call)
    );

  return (
    <ToolCard
      name={name}
      status={call.status}
      icon={<Icon size={13} />}
      duration={call.durationMs}
      actions={actions}
    >
      {body}
    </ToolCard>
  );
}
