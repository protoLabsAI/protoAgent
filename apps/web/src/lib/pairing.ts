/** Device pairing redemption (ADR 0087).
 *
 * A pairing URL looks like `…/app/#pair=<code>`. The code rides the FRAGMENT because
 * fragments are never sent to the server — they stay out of access logs, proxy logs and
 * `Referer` headers, which a query string would not.
 *
 * Redemption runs BEFORE React mounts, so the app never renders its "enter your token"
 * gate for a device that is about to be authorised.
 *
 * The same code can also be TYPED (ADR 0087 D3, amended 2026-10-18): a phone that can't scan
 * opens the console, gets the AuthGate, and enters the `XXXXX-XXXXX` code under "Have a
 * pairing code?". Both paths go through `claimPairingCode`, so the token lands in exactly
 * one place, the same strict way — its OWN key, `protoagent.deviceToken`, never the
 * operator's `protoagent.authToken` (#4336 S3, `lib/credentials.ts`).
 *
 * A successful claim also leaves a one-shot "paired as <name>" notice (`subscribePaired` /
 * `takePairedNotice`) for the console to confirm on the phone itself — the operator's screen
 * learns from its device list, but the phone otherwise just… opened. */

import { AGENT_CODE_LENGTH, formatAgentCode, isCompleteAgentCode, normalizeAgentCode } from "./agentPairing";
import { apiUrl } from "./api/routing";
import { storeDeviceCredential } from "./credentials";

/** A pairing code — device or agent — is 10 Crockford chars shown as `XXXXX-XXXXX`. The
 *  shape is shared with agent codes (one minting path server-side), so are the helpers. */
export const PAIRING_CODE_LENGTH = AGENT_CODE_LENGTH;
export const formatPairingCode = formatAgentCode;
export const normalizePairingCode = normalizeAgentCode;
export const isCompletePairingCode = isCompleteAgentCode;

export { migrateLegacyDeviceToken } from "./credentials";

/** What the phone is told after a successful claim. */
export type PairedNotice = { id: string; name: string };

let pairedNotice: PairedNotice | null = null;
const pairedListeners = new Set<() => void>();

/** Subscribe to "a claim just succeeded" (fires once per claim). */
export function subscribePaired(listener: () => void): () => void {
  pairedListeners.add(listener);
  return () => pairedListeners.delete(listener);
}

/** The pending "paired as …" notice, consumed: a second caller gets null, so one claim
 *  confirms once even under StrictMode's double effects. */
export function takePairedNotice(): PairedNotice | null {
  const n = pairedNotice;
  pairedNotice = null;
  return n;
}

function announcePaired(notice: PairedNotice) {
  pairedNotice = notice;
  pairedListeners.forEach((l) => l());
}

/** Read + immediately erase the `#pair=` code from the URL and history.
 *
 * Stripped whether or not the claim succeeds: a spent or rejected code left in the address
 * bar leaks into screenshots and back-button history for no benefit. `replaceState` (not
 * `pushState`) so Back doesn't walk into the pairing URL again. */
function takePairingCode(): string | null {
  const hash = window.location.hash || "";
  const match = hash.match(/[#&]pair=([A-Za-z0-9_-]+)/);
  if (!match) return null;
  const code = match[1];
  try {
    const clean = window.location.pathname + window.location.search;
    window.history.replaceState(null, "", clean);
  } catch {
    // A blocked history write must not stop the claim — worst case the URL keeps the code
    // until the next navigation.
  }
  return code;
}

/** A human-recognisable device name, so the operator's Devices list is readable.
 *
 * Best-effort from the UA — it only has to be recognisable in a short list, not accurate.
 * The server truncates and sanitises regardless. */
function deviceName(): string {
  const ua = navigator.userAgent || "";
  if (/iPhone/i.test(ua)) return "iPhone";
  if (/iPad/i.test(ua)) return "iPad";
  if (/Android/i.test(ua)) return /Mobile/i.test(ua) ? "Android phone" : "Android tablet";
  if (/Macintosh/i.test(ua)) return "Mac";
  if (/Windows/i.test(ua)) return "Windows PC";
  if (/Linux/i.test(ua)) return "Linux";
  return "Browser";
}

export type PairResult = { ok: true } | { ok: false; error: string } | null;
export type ClaimResult = { ok: true } | { ok: false; error: string; status?: number };

/** What a failed claim means, in words a person holding a phone can act on.
 *
 *  The server answers every bad code with ONE 403 (unknown / expired / already used / the
 *  pending codes were just dropped by the 5-miss lockout) — deliberately, so a prober learns
 *  nothing about which codes exist (ADR 0087 D4). The console can't tell those apart either,
 *  so it names all three causes and the one fix that covers them: a fresh code. */
export function describePairingError(res: { status?: number; error: string }): string {
  if (res.status === 403) {
    return (
      "That code didn't work. Check it against the screen it's shown on — or, if it expired " +
      "(codes last 5 minutes and work once) or several wrong tries cancelled it, make a new " +
      "one under Settings ▸ Devices ▸ Add a device, or with protoagent pair --device."
    );
  }
  if (res.status === undefined && /abort|timeout|timed out|failed to fetch|network|offline|load failed/i.test(res.error)) {
    return "Couldn't reach this agent. Check the phone is on the same Wi-Fi (or tailnet) and try again.";
  }
  return res.error;
}

/**
 * Redeem a pairing code for this device's token and store it.
 *
 * Deliberately NOT routed through `lib/api.ts`'s `request()`: that attaches the operator
 * bearer, and this request runs precisely when we don't have one. It is also the one endpoint
 * that must work unauthenticated (ADR 0087 D4). The URL still goes through `apiUrl`, so a
 * console served under a hub's per-agent prefix (`/app/agent/<slug>/`) or against an
 * explicit API base (the desktop shell) claims on the agent that minted the code, not on
 * whatever origin-root `/api` happens to be.
 */
export async function claimPairingCode(code: string): Promise<ClaimResult> {
  try {
    const res = await fetch(apiUrl("/api/pairing/claim"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name: deviceName() }),
      // First render waits on this, so it must not hang forever on a flaky network — the
      // pairing code outlives a retry (5 minutes) but a wedged request would strand the app.
      signal: AbortSignal.timeout(10_000),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data?.token) {
      return { ok: false, status: res.status, error: `HTTP ${res.status}: ${String(data?.error || "pairing failed")}` };
    }
    // Strict (ADR 0114 D1): a token the browser failed to keep must not report success —
    // the throw lands in the catch below and comes back as { ok: false }.
    // Its own key, never the operator's (#4336 S3).
    const id = typeof data?.device?.id === "string" ? data.device.id : undefined;
    storeDeviceCredential(data.token, id);
    const name = typeof data?.device?.name === "string" && data.device.name ? data.device.name : deviceName();
    announcePaired({ id: id ?? "", name });

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "pairing failed" };
  }
}

/**
 * Claim a pairing code if the URL carries one. Returns null when there's nothing to do.
 */
export async function redeemPairingFromUrl(): Promise<PairResult> {
  const code = takePairingCode();
  if (!code) return null;
  const res = await claimPairingCode(code);
  return res.ok ? res : { ok: false, error: res.error };
}
