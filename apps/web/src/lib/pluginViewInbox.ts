// Host → plugin-view deliveries (#3617): a console-side message addressed to one plugin view
// (`plugin:<id>:<view>`), held until that view's page is LISTENING, then posted into its
// iframe by PluginView.
//
// Why a queue: the caller usually opens the view in the same breath (a chat chip → "show
// the Artifact panel on v2"), and a collapsed dock UNMOUNTS its column — so at post time the
// iframe may not exist yet, or may exist but still be loading. PluginView drains the view's
// queue when the page announces `protoagent:ready` (the DS plugin-kit posts it from
// `initPluginView()`), and immediately for a page that already did. A page that wants these
// deliveries registers its own `message` listener BEFORE calling `initPluginView()`.
//
// In memory only, never persisted: a delivery is a "do this now" nudge, and it can carry
// what the operator was just looking at (incognito chats included) — it must not outlive
// the page load. Latest-wins per message type, so a burst of selects lands on the last.

export type PluginViewMessage = { type: string } & Record<string, unknown>;

const queues = new Map<string, PluginViewMessage[]>();
// STICKY context (#4076): state the page needs on EVERY load, not a one-shot nudge — e.g. which
// chat the Artifact panel belongs to. A plugin view's iframe unmounts with its dock and reloads
// fresh, so a queued message delivered once would be lost on the next mount. Latest-wins per
// type, in memory only (same reasons as the queue); re-queued each time the page says ready.
const contexts = new Map<string, Map<string, PluginViewMessage>>();
const listeners = new Set<(viewKey: string) => void>();

/** Queue `msg` for the plugin view `viewKey`. The type must be the plugin's own namespace —
 *  never the host bridge's `protoagent:*` protocol, which a delivery must not be able to fake. */
export function postToPluginView(viewKey: string, msg: PluginViewMessage): boolean {
  if (!viewKey.startsWith("plugin:") || !msg || typeof msg.type !== "string") return false;
  if (!msg.type || msg.type.startsWith("protoagent:")) return false;
  const q = (queues.get(viewKey) ?? []).filter((m) => m.type !== msg.type);
  q.push(msg);
  queues.set(viewKey, q);
  listeners.forEach((l) => l(viewKey));
  return true;
}

/** Set the sticky context `msg` for `viewKey`: delivered now (through the queue) AND again on
 *  every later page load (PluginView re-queues it on `protoagent:ready`). A change of value is
 *  delivered at once; the same value again is a no-op. Same namespace rule as postToPluginView. */
export function setPluginViewContext(viewKey: string, msg: PluginViewMessage): boolean {
  if (!viewKey.startsWith("plugin:") || !msg || typeof msg.type !== "string") return false;
  if (!msg.type || msg.type.startsWith("protoagent:")) return false;
  const ctx = contexts.get(viewKey) ?? new Map<string, PluginViewMessage>();
  const prev = ctx.get(msg.type);
  if (prev && JSON.stringify(prev) === JSON.stringify(msg)) return true;
  ctx.set(msg.type, msg);
  contexts.set(viewKey, ctx);
  return postToPluginView(viewKey, msg);
}

/** Re-queue every sticky context for `viewKey` (a freshly loaded page has none of it yet).
 *  Latest-wins per type, so a context still queued is not doubled. Doesn't notify: the caller
 *  flushes right after. */
export function requeuePluginViewContext(viewKey: string): void {
  const ctx = contexts.get(viewKey);
  if (!ctx || !ctx.size) return;
  let q = queues.get(viewKey) ?? [];
  for (const msg of ctx.values()) q = [...q.filter((m) => m.type !== msg.type), msg];
  queues.set(viewKey, q);
}

/** Take (and clear) everything queued for `viewKey`, oldest first. */
export function takePluginViewMessages(viewKey: string): PluginViewMessage[] {
  const q = queues.get(viewKey) ?? [];
  queues.delete(viewKey);
  return q;
}

/** Notified with the view key whenever something is queued. */
export function onPluginViewMessage(fn: (viewKey: string) => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Test-only: drop every queue. */
export function resetPluginViewInbox(): void {
  queues.clear();
  contexts.clear();
}
