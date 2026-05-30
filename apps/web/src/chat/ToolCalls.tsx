import { Check, ChevronRight, Loader2, Wrench, X } from "lucide-react";
import { useState } from "react";

import type { ToolCall } from "../lib/types";

/**
 * Renders the agent's tool activity as collapsible cards inside an assistant
 * message. Each card shows the tool name, a running→done/error state pill, and
 * (when expanded) the input preview + result preview the server streamed over
 * the tool-call DataPart. Mirrors ProtoMaker's chat tool-call cards.
 */
export function ToolCalls({ calls }: { calls: ToolCall[] }) {
  return (
    <div className="tool-calls">
      {calls.map((call) => (
        <ToolCard key={call.id} call={call} />
      ))}
    </div>
  );
}

function ToolCard({ call }: { call: ToolCall }) {
  // Done cards collapse by default; running cards stay open so the operator
  // can watch the input as it fires.
  const [open, setOpen] = useState(call.status === "running");
  const hasDetail = Boolean(call.input || call.output);

  return (
    <div className={`tool-card tool-card-${call.status}`}>
      <button
        type="button"
        className="tool-card-head"
        aria-expanded={open}
        disabled={!hasDetail}
        onClick={() => setOpen((v) => !v)}
      >
        {hasDetail ? (
          <ChevronRight size={13} className={`tool-card-caret${open ? " open" : ""}`} />
        ) : (
          <span className="tool-card-caret-spacer" />
        )}
        <Wrench size={13} className="tool-card-icon" />
        <span className="tool-card-name">{call.name}</span>
        <StatusGlyph status={call.status} />
      </button>
      {open && hasDetail ? (
        <div className="tool-card-body">
          {call.input ? (
            <div className="tool-card-section">
              <span className="tool-card-label">input</span>
              <pre>{call.input}</pre>
            </div>
          ) : null}
          {call.output ? (
            <div className="tool-card-section">
              <span className="tool-card-label">result</span>
              <pre>{call.output}</pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function StatusGlyph({ status }: { status: ToolCall["status"] }) {
  if (status === "running") return <Loader2 size={13} className="spin tool-card-status running" />;
  if (status === "error") return <X size={13} className="tool-card-status error" />;
  return <Check size={13} className="tool-card-status done" />;
}
