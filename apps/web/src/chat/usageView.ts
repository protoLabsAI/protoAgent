// Pure helpers behind the /usage system note — kept component-free, same convention as
// perfView.ts. Same data as the Settings ▸ Providers meters (`GET /api/usage`, ADR 0122),
// just chat-native: one line per window, every connected subscription.

import type { UsageSnapshot, UsageWindow } from "../lib/queries";

const ACCOUNT_LABEL: Record<string, string> = {
  "anthropic-oauth": "Claude subscription",
  "openai-codex": "ChatGPT / Codex subscription",
};

/** "2h 38m" / "5d 15h 8m" / "now" until `reset`. */
export function resetsIn(reset: number, now: number): string {
  const minutes = Math.max(0, Math.ceil((reset - now) / 60_000));
  if (!minutes) return "now";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  return `${days ? `${days}d ` : ""}${hours ? `${hours}h ` : ""}${minutes % 60}m`;
}

/** "Weekly · Fable — 17% used, resets in 5d 15h (near limit)". */
export function windowLine(w: UsageWindow, now: number): string {
  const scope = w.scope?.model || w.scope?.bucket;
  const label = `${w.kind.charAt(0).toUpperCase()}${w.kind.slice(1)}${scope ? ` · ${scope}` : ""}`;
  const reset = Date.parse(w.resets_at);
  if (!Number.isFinite(w.used_pct) || !Number.isFinite(reset)) return `${label} — unavailable`;
  const state = w.status === "blocked" || w.used_pct >= 100 ? " (limit reached)"
    : w.status === "warning" || w.used_pct >= 80 ? " (near limit)" : "";
  return `${label} — ${Math.round(w.used_pct)}% used, resets ${reset <= now ? "now" : `in ${resetsIn(reset, now)}`}${state}`;
}

/** The /usage system note. An account the provider told us nothing about says so
 *  rather than disappearing. */
export function usageNoteMarkdown(snapshot: UsageSnapshot | null, now: number = Date.now()): string {
  const accounts = snapshot?.accounts ?? [];
  if (!accounts.length) return "**Subscription usage**\n\nNo Claude or ChatGPT / Codex subscription is connected.";
  const blocks = accounts.map((a) => {
    const name = `**${ACCOUNT_LABEL[a.provider_id] ?? a.provider_id}**`;
    if (a.status !== "ok" || !a.windows?.length) return `${name}\n- Usage unavailable`;
    const stale = a.stale ? " (last known, refresh failed)" : "";
    return `${name}${stale}\n${a.windows.map((w) => `- ${windowLine(w, now)}`).join("\n")}`;
  });
  return `**Subscription usage**\n\n${blocks.join("\n\n")}`;
}
