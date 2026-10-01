import type { ChatMessage, DelegateProgress, DelegateProgressEvent, DelegateTool } from "../lib/types";

// Landing a coding delegate's live-progress snapshot (#3979) on the card it belongs to.
// Pure — the live stream, the reattach stream and durable hydration all apply it the same
// way. A snapshot is the WHOLE state (latest wins), so applying one is a replace, never a
// merge, and a missed frame costs nothing but latency.

function strip(evt: DelegateProgressEvent): DelegateProgress {
  const { id: _id, ...progress } = evt;
  void _id;
  return progress;
}

/** Put `evt` on its card: an `@` mention card (a tool call with that id — one card can
 *  address several delegates, so it is keyed by target), else a `delegate_to` ask row
 *  (`delegation.id`). Searches newest-first: a mention card's id is stable per target set
 *  (`mention:claude-code`), so an earlier turn's card can share it, and the live one is
 *  always the latest. Returns the SAME array when nothing matched, so a caller can skip a
 *  store write. */
export function applyDelegateProgress(messages: ChatMessage[], evt: DelegateProgressEvent): ChatMessage[] {
  const progress = strip(evt);
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    const call = m.toolCalls?.find((c) => c.id === evt.id);
    if (call) {
      const next = [...messages];
      next[i] = {
        ...m,
        toolCalls: m.toolCalls!.map((c) =>
          c === call ? { ...c, delegateProgress: { ...(c.delegateProgress ?? {}), [progress.target]: progress } } : c,
        ),
      };
      return next;
    }
    if (m.delegation?.id === evt.id) {
      const next = [...messages];
      next[i] = { ...m, delegation: { ...m.delegation, progress } };
      return next;
    }
  }
  return messages;
}

/** A turn ended: any delegation row whose delegate never sent its final snapshot (the
 *  turn was stopped, the stream dropped) stops reading as live. Mention cards need no
 *  help — their card's own status (settled by `settleStreamEnd`) is what they key off.
 *  Returns the SAME array when nothing changed. */
export function settleDelegateProgress(messages: ChatMessage[]): ChatMessage[] {
  let changed = false;
  const next = messages.map((m) => {
    const p = m.delegation?.progress;
    if (!p || p.done) return m;
    changed = true;
    return { ...m, delegation: { ...m.delegation, progress: { ...p, done: true, ok: false } } };
  });
  return changed ? next : messages;
}

/** The same, for a single message (durable hydration accumulates one at a time). */
export function applyDelegateProgressTo(message: ChatMessage, evt: DelegateProgressEvent): ChatMessage {
  return applyDelegateProgress([message], evt)[0];
}

/** A file location as the card shows it: the last two path segments (`src/calc.py:12`).
 *  The full path rides the title attribute. */
export function shortLocation(loc: { path: string; line?: number }): string {
  const parts = loc.path.split(/[\\/]/).filter(Boolean);
  const tail = parts.slice(-2).join("/") || loc.path;
  return loc.line !== undefined && loc.line > 0 ? `${tail}:${loc.line}` : tail;
}

/** The one line a tool reads as: its title, plus where it is working when the title
 *  doesn't already say so. */
export function toolLine(t: DelegateTool): string {
  const loc = t.locations?.[0];
  if (!loc) return t.name;
  const short = shortLocation(loc);
  const file = short.split(":")[0].split("/").pop() || short;
  return t.name.includes(file) ? t.name : `${t.name} · ${short}`;
}

/** How far through its plan a delegate is: `2/5`, or null with no plan. */
export function planProgress(p: DelegateProgress): string | null {
  if (!p.plan?.length) return null;
  const done = p.plan.filter((e) => e.status === "completed").length;
  return `${done}/${p.plan.length}`;
}
