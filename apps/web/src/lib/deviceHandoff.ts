/** Moving a paired phone between the agent's addresses (ADR 0121 D1, #4336 S7.5).
 *
 * A phone has ONE home origin: its token, transcripts and home-screen icon live there. It
 * keeps the agent's other origins (the address set, `credentials.deviceAddresses`) for two
 * things:
 *
 * - **Unreachable home.** The offline notice lists the other origins as plain links, each with
 *   what the phone needs for it. Opening one means pairing again there — a token never
 *   crosses origins, and CORS is not widened to make one page call another.
 * - **Moving home in one tap.** From the CURRENT home, `POST /api/devices/self/handoff` mints a
 *   60 s single-use code bound to this device; the console navigates to
 *   `<origin>/app/#pair=<code>`, and claiming it there rotates this device's token instead of
 *   adding a row. The old origin's token stays valid until the new one is used (so a failed
 *   move strands nothing), then the server reports it `handed_off` and this origin forgets it
 *   (`deviceRotation.ts`). It is never presented late enough to look like token theft.
 *
 * The bearer is sent explicitly and to the HUB (`host: true`), like rotation: it is the hub's
 * registry that knows this device, whichever fleet member is focused. */

import { apiUrl } from "./api/routing";
import { deviceToken, markDeviceMoved } from "./credentials";

import type { DeviceAddress } from "./credentials";

/** What the address is, in a few words. */
export function addressLabel(a: Pick<DeviceAddress, "kind">): string {
  switch (a.kind) {
    case "tailnet_https":
      return "Tailnet HTTPS";
    case "public":
      return "Public URL";
    case "tailnet":
      return "Tailnet";
    default:
      return "Wi-Fi";
  }
}

/** What the phone needs for the address to work. */
export function requirementText(a: Pick<DeviceAddress, "requirement">): string {
  if (a.requirement === "tailscale") return "needs the Tailscale app on this phone";
  if (a.requirement === "same_network") return "only on the same Wi-Fi as the agent";
  return "works from any network";
}

/** A host to show (the origin without its scheme). */
export function addressHost(origin: string): string {
  return origin.replace(/^https?:\/\//, "");
}

/** The console at another origin — a plain link target (the offline notice). */
export function consoleUrl(origin: string): string {
  return `${origin}/app/`;
}

export type HandoffResult = { ok: true; url: string } | { ok: false; error: string };

/** Ask the home origin for a handoff code to `origin`. Never throws. */
export async function requestHandoff(origin: string): Promise<HandoffResult> {
  const token = deviceToken();
  if (!token) return { ok: false, error: "This browser isn't a paired device, so there's nothing to move." };
  try {
    const res = await fetch(apiUrl("/api/devices/self/handoff", { host: true }), {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ origin }),
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    const data = (await res.json().catch(() => ({}))) as { url?: unknown; origin?: unknown; detail?: unknown; error?: unknown };
    if (!res.ok) {
      if (res.status === 404 && data?.error !== "not_a_device") {
        return { ok: false, error: "This agent is too old to move a phone. Update it, or pair at the other address." };
      }
      const detail = typeof data?.detail === "string" ? data.detail : `HTTP ${res.status}`;
      return { ok: false, error: detail.charAt(0).toUpperCase() + detail.slice(1) + "." };
    }
    // Trust only a URL on the origin we asked for, with the code in the fragment.
    const url = typeof data?.url === "string" ? data.url : "";
    if (!url.startsWith(`${origin}/app/#pair=`)) return { ok: false, error: "The agent answered with an unexpected address." };
    return { ok: true, url };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "Couldn't reach the agent." };
  }
}

/** Leave for the new home: record where this phone went (for the old origin's AuthGate),
 *  then navigate. `navigate` is a seam for tests. */
export function goToHandoff(origin: string, url: string, navigate: (url: string) => void = (u) => window.location.assign(u)): void {
  markDeviceMoved(origin);
  navigate(url);
}

/** A reachability HINT for another origin (ADR 0121 D1): resolves true when something answers
 *  there. `no-cors` gives an opaque response — enough to say "it answered", nothing more —
 *  and an https page can't probe an http origin at all (mixed content), so false means only
 *  "unknown", never "down". */
export async function probeOrigin(origin: string, timeoutMs = 3000): Promise<boolean> {
  try {
    await fetch(`${origin}/healthz`, { mode: "no-cors", cache: "no-store", signal: AbortSignal.timeout(timeoutMs) });
    return true;
  } catch {
    return false;
  }
}
