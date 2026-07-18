/** Device pairing redemption (ADR 0087).
 *
 * A pairing URL looks like `…/app/#pair=<code>`. The code rides the FRAGMENT because
 * fragments are never sent to the server — they stay out of access logs, proxy logs and
 * `Referer` headers, which a query string would not.
 *
 * Redemption runs BEFORE React mounts, so the app never renders its "enter your token"
 * gate for a device that is about to be authorised. */

const KEY = "protoagent.authToken";
const DEVICE_KEY = "protoagent.deviceId";

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

/**
 * Claim a pairing code if the URL carries one. Returns null when there's nothing to do.
 *
 * Deliberately NOT routed through `lib/api.ts`: that attaches the operator bearer, and this
 * request runs precisely when we don't have one. It is also the one endpoint that must work
 * unauthenticated (ADR 0087 D4).
 */
export async function redeemPairingFromUrl(): Promise<PairResult> {
  const code = takePairingCode();
  if (!code) return null;
  try {
    const res = await fetch("/api/pairing/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name: deviceName() }),
      // First render waits on this, so it must not hang forever on a flaky network — the
      // pairing code outlives a retry (120s) but a wedged request would strand the app.
      signal: AbortSignal.timeout(10_000),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data?.token) {
      return { ok: false, error: String(data?.error || "pairing failed") };
    }
    window.localStorage.setItem(KEY, data.token);
    if (data?.device?.id) window.localStorage.setItem(DEVICE_KEY, data.device.id);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "pairing failed" };
  }
}
