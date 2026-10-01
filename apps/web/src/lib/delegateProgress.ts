import type { DelegatePlanEntry, DelegateProgress, DelegateProgressEvent, DelegateTool } from "./types";

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
