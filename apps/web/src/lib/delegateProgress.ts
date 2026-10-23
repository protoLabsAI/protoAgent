import type {
  ChatPart,
  DelegatePlanEntry,
  DelegateProgress,
  DelegateProgressEvent,
  DelegateTimelineEntry,
  DelegateTool,
  ToolCall,
} from "./types";

// Wire decoding for a coding delegate's live-progress snapshot (#3979). The server
// (graph/delegate_progress.py) sends snake_case and already bounds every list; this
// re-checks every field anyway — a frame is untrusted input to the renderer, and an older
// or forked producer may send less. Re-capped here too, so a misbehaving producer can't
// grow the card.

const PLAN_MAX = 20;
const RECENT_MAX = 6;
const LOCATIONS_MAX = 3;
const TEXT_MAX = 400;

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function toolFromWire(raw: unknown): DelegateTool | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const t = raw as Record<string, unknown>;
  const name = str(t.name);
  if (!name) return undefined;
  const locations = Array.isArray(t.locations)
    ? t.locations
        .flatMap((l) => {
          if (!l || typeof l !== "object") return [];
          const loc = l as { path?: unknown; line?: unknown };
          if (typeof loc.path !== "string" || !loc.path) return [];
          return [typeof loc.line === "number" ? { path: loc.path, line: Math.floor(loc.line) } : { path: loc.path }];
        })
        .slice(0, LOCATIONS_MAX)
    : [];
  return {
    ...(str(t.id) ? { id: str(t.id) } : {}),
    name,
    ...(str(t.kind) ? { kind: str(t.kind) } : {}),
    status: str(t.status) || "running",
    ...(locations.length ? { locations } : {}),
  };
}

/** A progress payload off the wire (or a bus event's `progress`), or null when it isn't one. */
export function delegateProgressFromWire(raw: unknown, id?: string): DelegateProgressEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const d = raw as Record<string, unknown>;
  const key = id ?? str(d.id);
  const target = str(d.target);
  if (!key || !target) return null;
  const plan: DelegatePlanEntry[] | undefined = Array.isArray(d.plan)
    ? d.plan
        .flatMap((e) => {
          if (!e || typeof e !== "object") return [];
          const entry = e as { content?: unknown; status?: unknown };
          const content = str(entry.content);
          return content ? [{ content, status: str(entry.status) || "pending" }] : [];
        })
        .slice(0, PLAN_MAX)
    : undefined;
  const recentTools = Array.isArray(d.recent_tools)
    ? d.recent_tools.flatMap((t) => toolFromWire(t) ?? []).slice(-RECENT_MAX)
    : [];
  const progress: DelegateProgress = {
    target,
    ...(plan && plan.length ? { plan } : {}),
    ...(toolFromWire(d.current_tool) ? { currentTool: toolFromWire(d.current_tool) } : {}),
    recentTools,
    toolCount: typeof d.tool_count === "number" ? Math.max(0, Math.floor(d.tool_count)) : recentTools.length,
    ...(str(d.text) ? { text: str(d.text).slice(-TEXT_MAX) } : {}),
    done: d.done === true,
    ok: d.ok !== false,
  };
  return { ...progress, id: key };
}

// ── A delegate reply's own timeline ───────────────────────────────────────────
// A coding delegate's reply text is its narration with every action cut out of it ("Now
// executing the edits:" … "The tests pass."), so the server sends the run in order on the
// reply frame — text runs and tool calls, as they happened (graph/delegate_progress.py,
// `reply_timeline`) — and the reply bubble renders it like a native turn: text ↔ tool cards.

const TIMELINE_MAX = 300;
const TIMELINE_TEXT_MAX = 200_000;
const TIMELINE_IO_MAX = 2000;

/** Decode a reply frame's `timeline`, or undefined when it is absent, malformed, or has no
 *  tool call in it (then the plain reply text is the whole story). */
export function delegateTimelineFromWire(raw: unknown): DelegateTimelineEntry[] | undefined {
  if (!Array.isArray(raw) || raw.length > TIMELINE_MAX) return undefined;
  const out: DelegateTimelineEntry[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") return undefined;
    const e = item as Record<string, unknown>;
    if (e.type === "text") {
      const text = str(e.text).slice(0, TIMELINE_TEXT_MAX);
      if (text.trim()) out.push({ type: "text", text });
    } else if (e.type === "tool") {
      const name = str(e.name);
      if (!name) return undefined;
      out.push({
        type: "tool",
        id: str(e.id) || `t${out.length}`,
        name,
        kind: str(e.kind) || undefined,
        status: e.status === "failed" ? "failed" : "completed",
        input: str(e.input).slice(0, TIMELINE_IO_MAX) || undefined,
        output: str(e.output).slice(0, TIMELINE_IO_MAX) || undefined,
      });
    } else {
      return undefined;
    }
  }
  return out.some((e) => e.type === "tool") ? out : undefined;
}

/** The ordered `parts` + `toolCalls` a reply bubble renders its timeline with. Tool ids are
 *  scoped by `scope` (the bubble's id): the coder's own ids repeat across its runs, and a
 *  card id is what a dismissal or a `tools` part keys on. Adjacent calls share one group. */
export function delegateTimelineMessage(
  timeline: DelegateTimelineEntry[],
  scope: string,
): { parts: ChatPart[]; toolCalls: ToolCall[] } {
  const parts: ChatPart[] = [];
  const toolCalls: ToolCall[] = [];
  for (const entry of timeline) {
    if (entry.type === "text") {
      parts.push({ kind: "text", text: entry.text });
      continue;
    }
    const id = `${scope}:${entry.id}:${toolCalls.length}`;
    toolCalls.push({
      id,
      name: entry.name,
      input: entry.input,
      output: entry.output,
      status: entry.status === "failed" ? "error" : "done",
      delegated: entry.kind ? { kind: entry.kind } : {},
    });
    const last = parts[parts.length - 1];
    if (last?.kind === "tools") last.ids.push(id);
    else parts.push({ kind: "tools", ids: [id] });
  }
  return { parts, toolCalls };
}
