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

import { ToolCard, ToolCardList, ToolSection } from "@protolabsai/ui/tool-card";

import type { ToolCall } from "../lib/types";
import { ToolCardSummary } from "./ToolCardSummary";
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
  onCancelDelegation,
}: {
  calls: ToolCall[];
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
  // Spotlight the active work: running top-level cards render in full; finished ones
  // fold into a single expandable summary chip ("N tools"), so a fan-out turn doesn't
  // bury the answer under a wall of cards. "Settled" is the card's own status — a
  // running subagent `task` stays full so its nested children stay visible. Once the
  // turn ends nothing is running, so the whole run collapses into the chip.
  const running = top.filter((c) => c.status === "running");
  const settled = top.filter((c) => c.status !== "running");
  const failedCount = settled.filter((c) => c.status === "error").length;
  // A lone finished tool isn't clutter — fold only once there are ≥2 settled cards, so a
  // simple one-tool turn still reads as a normal card rather than a pointless "1 tool" chip.
  const fold = settled.length >= 2;

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

  // Common case (no fan-out): render every card inline in emission order — identical to a
  // flat list, so a card never changes position (and never loses its expanded state) as it
  // settles. Only a real fan-out (≥2 finished) splits running-vs-folded.
  if (!fold) {
    return <ToolCardList className="tool-calls">{top.map(group)}</ToolCardList>;
  }

  return (
    <ToolCardList className="tool-calls">
      {running.map(group)}
      <ToolCardSummary
        // Running total for the block — counts every tool call (running + settled), so
        // the tally ticks up live as tools fire, not just what's currently folded.
        count={top.length}
        status={failedCount > 0 ? "error" : "done"}
        failedCount={failedCount || undefined}
      >
        {settled.map(group)}
      </ToolCardSummary>
    </ToolCardList>
  );
}

/** A tool card plus, when it's a subagent `task`, its nested child tool cards.
 *  Subagent nesting rides the DS `ToolCard` `nested` prop (indented child rail). */
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
  const nested = kids?.length
    ? kids.map((kid) => (
        // Children inherit no cancel callback — they aren't independent delegations.
        <ToolGroup key={kid.id} call={kid} childrenByParent={childrenByParent} />
      ))
    : undefined;

  // Collapsed by default and stays put — the header row (icon, name, status) is
  // the stable at-a-glance view; expanding is an explicit, sticky choice so the
  // message doesn't reflow as tools start and finish. Pass `children` only when
  // there's detail so the DS gives us the disabled-caret behavior for empty cards
  // (the DS gates `hasBody` on `children != null`, so a no-detail card omits it).
  const Icon = iconFor(call.name);
  const body =
    call.input || call.output ? (
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

  return (
    <ToolCard
      name={cardLabel(call)}
      status={call.status}
      icon={<Icon size={13} />}
      duration={call.durationMs}
      nested={nested}
      actions={actions}
    >
      {body}
    </ToolCard>
  );
}
