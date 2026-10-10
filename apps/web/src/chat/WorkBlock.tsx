import { BookOpen, Brain, Wrench } from "lucide-react";
import type { ReactNode } from "react";

import { ToolCard } from "@protolabsai/ui/tool-card";
import { Tooltip } from "@protolabsai/ui/overlays";

import { StreamingPreview } from "../artifacts/StreamingPreview";
import type { ChatPart, ComponentSpec, ToolCall } from "../lib/types";
import { ChatComponent } from "./ChatComponent";
import { toolsForGroup } from "./parts";
import { ReasoningCard } from "./ReasoningCard";
import type { ToolArgsBuffer } from "./toolArgsBuffer";
import { ToolCalls } from "./ToolCalls";

type ToolsPart = Extract<ChatPart, { kind: "tools" }>;

/** Guess the artifact kind from the streamed markup, for while the tool's full args have NOT yet
 *  arrived. The real server streams the `code` argument alone, with the call's `input` empty until
 *  model end (server/turn_stream.py: `_on_chat_model_stream` sends `input: ""`, the full args ride
 *  a second `tool_start` at `_on_chat_model_end`). So the live buffer is all we have — and only
 *  `html`/`svg` are ever previewable, both just markup. Anything opening with a tag reads as html,
 *  an `<svg`/`<?xml` opener as svg; a non-markup body (a react/mermaid/json/markdown artifact) reads
 *  as "" so {@link StreamingPreview} shows its placeholder, never a frame over code that isn't HTML. */
function sniffArtifactKind(code: string): string {
  const head = code.replace(/^\s+/, "").slice(0, 64).toLowerCase();
  if (head.startsWith("<svg") || head.startsWith("<?xml")) return "svg";
  if (head.startsWith("<")) return "html";
  return "";
}

/** A streaming `show_artifact` call whose declared `code` is arriving live (S3): the one tool call
 *  that gets a LIVE PREVIEW in the spotlight instead of the plain running card (ADR 0118 D3, S8c).
 *  Returns the preview's inputs, or null for a non-artifact tool / one with no buffered args yet /
 *  one KNOWN to be non-inline.
 *
 *  Keyed on the live BUFFER, never on `call.input`: the real server leaves `input` empty for the
 *  whole stream and only fills it (kind/placement/title) with a second `tool_start` at model end,
 *  AFTER the final `done` slice — so parsing `input` would leave the operator on a spinner until the
 *  write was already finished (the bug this card fixes). The kind is sniffed from the markup while
 *  the args are absent, then the real kind/title replace it once the full args land; a placement
 *  that is KNOWN and not `inline` drops the preview (an unknown placement, mid-stream, does not). */
export function inlineArtifactPreview(
  call: ToolCall | undefined,
  toolArgs: Record<string, ToolArgsBuffer> | undefined,
): { buffer: ToolArgsBuffer; kind: string; title: string } | null {
  if (!call || call.name !== "show_artifact") return null;
  const buffer = toolArgs?.[call.id];
  if (!buffer) return null;
  let args: { kind?: unknown; placement?: unknown; title?: unknown } = {};
  if (call.input) {
    try {
      args = JSON.parse(call.input) as typeof args;
    } catch {
      args = {}; // still mid-stream / not valid JSON yet — fall back to the buffer
    }
  }
  // Drop the preview only once placement is KNOWN and not inline; an absent placement (the whole
  // stream, until model end) keeps the preview up so the operator watches the artifact being written.
  if (typeof args.placement === "string" && args.placement !== "inline") return null;
  const kind = (typeof args.kind === "string" && args.kind) || sniffArtifactKind(buffer.text);
  const title = typeof args.title === "string" ? args.title : "";
  return { buffer, kind, title };
}

/** The single tool call the WorkBlock spotlights while streaming — the most-recent tool, i.e. the
 *  last id of the last `tools` group. Exported so ChatMessageView decides the S8c handover off the
 *  SAME call the spotlight previews, and the two can never pick different tools. */
export function spotlightToolId(parts: ChatPart[]): string | undefined {
  const toolsParts = parts.filter((p): p is ToolsPart => p.kind === "tools");
  const last = toolsParts[toolsParts.length - 1];
  return last && last.ids.length ? last.ids[last.ids.length - 1] : undefined;
}

/** The inline (S7b) `artifact-ref` component this turn's streaming `show_artifact` hands its preview
 *  over to, or null. The one correlation available: a live turn has at most one streaming inline
 *  artifact and at most one inline ref lands for it — there is no shared id on the wire. Exported so
 *  ChatMessageView pulls this ref OUT of the answer while the spotlight owns it (else a second frame
 *  stacks below the preview) and feeds it back as the handover target. */
export function findInlineArtifactRef(parts: ChatPart[]): ComponentSpec | null {
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    if (p.kind === "component" && p.spec.component === "artifact-ref" && p.spec.props.inline === true) return p.spec;
  }
  return null;
}

/** A skill load is a `load_skill` tool call; the skill name rides its JSON input. */
function skillName(input?: string): string {
  if (!input) return "skill";
  try {
    const parsed = JSON.parse(input) as { name?: unknown };
    return typeof parsed.name === "string" ? parsed.name : "skill";
  } catch {
    return "skill";
  }
}

function plural(n: number, one: string): string {
  return `${n} ${one}${n === 1 ? "" : "s"}`;
}

/**
 * Folds an agentic turn's intermediate reason→tool timeline behind ONE collapsed disclosure
 * so the final answer leads. The header tallies the WORK done this turn — reasoning steps,
 * tool calls, and skill loads — each as an icon + count, with a hover breakdown. WHILE
 * STREAMING it keeps the most-recent tool exposed below the summary (kept until a newer tool
 * replaces it) so the block isn't fully collapsed while the agent works. Expand to replay
 * the full timeline.
 */
export function WorkBlock({
  parts,
  toolCalls,
  toolArgs,
  streaming,
  renderFinal,
}: {
  parts: ChatPart[];
  toolCalls?: ToolCall[];
  /** Live streamed tool-argument previews, keyed by tool-call id (ADR 0118 D3). Present only on a
   *  live turn; a streaming inline `show_artifact` call with a buffer here gets a live preview in
   *  the spotlight. Absent/empty → every card renders exactly as before. */
  toolArgs?: Record<string, ToolArgsBuffer>;
  streaming: boolean;
  /** The S8c handover: once the streaming inline artifact's ref has landed, the spotlight preview
   *  swaps (on `done`) for the artifact's own inline frame at the last measured height so the slot
   *  does not jump. Supplied by ChatMessageView (which owns both the spotlight and the answer, so it
   *  also keeps the ref out of the answer while this renders it). Absent → the preview just settles. */
  renderFinal?: (height: number) => ReactNode;
}) {
  // Tally the turn's work. Tool ids come from the timeline; resolve each to its call so we
  // can split plain tool calls from `load_skill` (skill loads get their own count).
  const callById = new Map((toolCalls ?? []).map((c) => [c.id, c]));
  const toolIds = new Set<string>();
  for (const p of parts) if (p.kind === "tools") for (const id of p.ids) toolIds.add(id);

  const toolTally = new Map<string, number>();
  const skillNames: string[] = [];
  for (const id of toolIds) {
    const call = callById.get(id);
    if (!call) continue;
    if (call.name === "load_skill") skillNames.push(skillName(call.input));
    else toolTally.set(call.name, (toolTally.get(call.name) ?? 0) + 1);
  }
  const toolCount = [...toolTally.values()].reduce((a, b) => a + b, 0);
  const skillCount = skillNames.length;
  const reasoningCount = parts.filter((p) => p.kind === "reasoning" && p.text.trim()).length;

  const label = streaming ? "Working…" : "Worked";
  const toolList = [...toolTally.entries()].map(([n, c]) => (c > 1 ? `${n} ×${c}` : n)).join(", ");

  // Hover breakdown — the lines behind the icon counts.
  const breakdown = (
    <div className="work-breakdown">
      {reasoningCount > 0 && <div>{plural(reasoningCount, "reasoning step")}</div>}
      {toolCount > 0 && (
        <div>
          {plural(toolCount, "tool call")}
          {toolList && <span className="work-breakdown-detail"> · {toolList}</span>}
        </div>
      )}
      {skillCount > 0 && (
        <div>
          {plural(skillCount, "skill load")}
          <span className="work-breakdown-detail"> · {skillNames.join(", ")}</span>
        </div>
      )}
    </div>
  );

  const header = (
    <Tooltip label={breakdown} side="top">
      <span className="work-stats">
        <span className="work-stat-label">{label}</span>
        {reasoningCount > 0 && (
          <span className="work-stat" aria-label={plural(reasoningCount, "reasoning step")}>
            <Brain size={12} />
            {reasoningCount}
          </span>
        )}
        {toolCount > 0 && (
          <span className="work-stat" aria-label={plural(toolCount, "tool call")}>
            <Wrench size={12} />
            {toolCount}
          </span>
        )}
        {skillCount > 0 && (
          <span className="work-stat" aria-label={plural(skillCount, "skill load")}>
            <BookOpen size={12} />
            {skillCount}
          </span>
        )}
      </span>
    </Tooltip>
  );

  // While streaming, spotlight ONLY the most-recent tool below the collapsed summary — the
  // reasoning, interstitial narration, and the streaming answer stay folded in the disclosure
  // (and the answer lands below the "Worked" summary once the turn settles). This is what keeps
  // a chatty/tool-heavy turn reading as one clean batch instead of interim text + split groups.
  let spotlightIds: string[] = [];
  if (streaming) {
    const id = spotlightToolId(parts);
    if (id) spotlightIds = [id];
  }

  // When the spotlit tool is a streaming inline `show_artifact`, the spotlight hosts a LIVE PREVIEW
  // of the artifact as the model writes it (S8c), instead of the plain running card. Every other
  // spotlit tool keeps the usual spotlight card.
  const spotlightPreview = inlineArtifactPreview(spotlightIds.length ? callById.get(spotlightIds[0]) : undefined, toolArgs);

  return (
    <div className="work">
      <ToolCard name={header} status={streaming ? "running" : "done"} className="work-block">
        <div className="work-timeline">
          {parts.map((part, i) =>
            part.kind === "reasoning" ? (
              part.text.trim() ? <ReasoningCard key={i} text={part.text} /> : null
            ) : part.kind === "tools" ? (
              <ToolCalls key={i} calls={toolsForGroup(part.ids, toolCalls)} flat />
            ) : part.kind === "component" ? (
              <ChatComponent key={i} spec={part.spec} />
            ) : part.text.trim() ? (
              <div key={i} className="work-text">{part.text}</div>
            ) : null,
          )}
        </div>
      </ToolCard>
      {spotlightIds.length > 0 ? (
        <div className="work-spotlight">
          {spotlightPreview ? (
            <StreamingPreview
              buffer={spotlightPreview.buffer}
              kind={spotlightPreview.kind}
              title={spotlightPreview.title}
              renderFinal={renderFinal}
            />
          ) : (
            <ToolCalls calls={toolsForGroup(spotlightIds, toolCalls)} spotlight />
          )}
        </div>
      ) : null}
    </div>
  );
}
