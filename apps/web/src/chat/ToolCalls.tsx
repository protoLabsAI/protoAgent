import {
  Calculator,
  Check,
  ChevronRight,
  Clock,
  Copy,
  Database,
  Globe,
  Loader2,
  Network,
  Search,
  Wrench,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";

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
  // Collapsed by default and stays put — the header row (icon, name, status)
  // is the stable at-a-glance view; expanding is an explicit, sticky choice so
  // the message doesn't reflow as tools start and finish. The user opens the
  // cards they care about.
  const [open, setOpen] = useState(false);
  const hasDetail = Boolean(call.input || call.output);
  const Icon = iconFor(call.name);

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
        <Icon size={13} className="tool-card-icon" />
        <span className="tool-card-name">{call.name}</span>
        {call.durationMs !== undefined ? (
          <span className="tool-card-dur">{formatDuration(call.durationMs)}</span>
        ) : null}
        <StatusGlyph status={call.status} />
      </button>
      {open && hasDetail ? (
        <div className="tool-card-body">
          {call.input ? (
            <ToolSection label="input" raw={call.input} role="input" tool={call.name} />
          ) : null}
          {call.output ? (
            <ToolSection label="result" raw={call.output} role="output" tool={call.name} />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ToolSection({
  label,
  raw,
  role,
  tool,
}: {
  label: string;
  raw: string;
  role: "input" | "output";
  tool: string;
}) {
  return (
    <div className="tool-card-section">
      <div className="tool-section-head">
        <span className="tool-card-label">{label}</span>
        <CopyButton text={raw} />
      </div>
      <ToolValue raw={raw} role={role} tool={tool} />
    </div>
  );
}

/** Copies the raw value to the clipboard, flashing a check on success. */
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="tool-copy"
      title="Copy to clipboard"
      aria-label={copied ? "Copied" : "Copy"}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          // Clipboard unavailable (insecure context / denied) — no-op.
        }
      }}
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}

/** Human-readable elapsed: "820ms" under a second, "1.2s" above. */
function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function StatusGlyph({ status }: { status: ToolCall["status"] }) {
  if (status === "running") return <Loader2 size={13} className="spin tool-card-status running" />;
  if (status === "error") return <X size={13} className="tool-card-status error" />;
  return <Check size={13} className="tool-card-status done" />;
}
