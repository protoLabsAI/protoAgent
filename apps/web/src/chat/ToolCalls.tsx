import "./tool-calls.css";
import {
  Calculator,
  Clock,
  Database,
  Globe,
  Network,
  Search,
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
export function ToolCalls({ calls }: { calls: ToolCall[] }) {
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
        <ToolGroup key={call.id} call={call} childrenByParent={childrenByParent} />
      ))}
    </ToolCardList>
  );
}

/** A tool card plus, when it's a subagent `task`, its nested child tool cards.
 *  Subagent nesting rides the DS `ToolCard` `nested` prop (indented child rail). */
function ToolGroup({
  call,
  childrenByParent,
}: {
  call: ToolCall;
  childrenByParent: Map<string, ToolCall[]>;
}) {
  const kids = childrenByParent.get(call.id);
  const nested = kids?.length
    ? kids.map((kid) => (
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

  return (
    <ToolCard
      name={call.name}
      status={call.status}
      icon={<Icon size={13} />}
      duration={call.durationMs}
      nested={nested}
    >
      {body}
    </ToolCard>
  );
}
