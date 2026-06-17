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

import { ToolCard, ToolCardList, ToolSection } from "@protolabsai/ui/tool-card";

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
  return (
    <ToolCardList className="tool-calls">
      {top.map((call) => (
        // Only TOP-LEVEL groups get the cancel callback — a delegation is always a
        // top-level `task`; its nested children (the subagent's own tools) aren't
        // independently cancellable, so recursion drops `onCancelDelegation`.
        <ToolGroup
          key={call.id}
          call={call}
          childrenByParent={childrenByParent}
          onCancelDelegation={onCancelDelegation}
        />
      ))}
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
      name={call.name}
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
