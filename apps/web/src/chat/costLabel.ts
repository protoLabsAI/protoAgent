// Provider-aware cost presentation (#2463). For native OAuth subscription
// providers (ADR 0097) the per-turn dollar value is a NOMINAL estimate computed
// from API pricing tables (including a fallback rate for unrecognized models) —
// the turn rode the user's ChatGPT/Claude subscription and generated no charge.
// Presenting that number as plain "Cost" reads as a bill; every surface that
// shows it must label it as an API-equivalent comparison instead.

/** The native OAuth subscription providers (mirror of graph/providers NATIVE_OAUTH_PROVIDERS). */
const SUBSCRIPTION_PROVIDERS = new Set(["anthropic-oauth", "openai-codex"]);

export function isSubscriptionProvider(provider: string | null | undefined): boolean {
  return SUBSCRIPTION_PROVIDERS.has((provider || "").trim().toLowerCase());
}

/** Footer dollars: "~" marks an estimate so a glance never reads it as a charge. */
export function costPrefix(subscription: boolean): string {
  return subscription ? "~" : "";
}

export function costAriaLabel(subscription: boolean): string {
  return subscription ? "estimated API-equivalent cost — not a charge" : "cost";
}

export function costTipLabel(subscription: boolean): string {
  return subscription ? "Est. cost" : "Cost";
}

export function costTipSub(subscription: boolean): string | undefined {
  return subscription ? "API-equivalent — not an additional charge" : undefined;
}

export function usageTipNote(subscription: boolean): string {
  return subscription
    ? "Context is the live prompt size. Dollars compare this turn to API pricing — " +
        "your subscription covered it; nothing extra was charged."
    : "Context is the live prompt size; cost is summed across the turn's calls.";
}
