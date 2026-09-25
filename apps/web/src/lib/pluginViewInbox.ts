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
}
