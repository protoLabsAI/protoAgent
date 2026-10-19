/** How a phone reaches this agent (#4336 S4, ADR 0121 D1) — the pure bits of the Devices
 *  panel's "Add a device" reach section, unit-tested without rendering it.
 *
 *  Best first: tailnet HTTPS (`tailscale serve`) → the operator's public URL
 *  (`network.public_console_url`, ADR 0121 D2) → the tailnet NAME over http → a tailnet IP →
 *  a LAN IP. The server already orders them; these helpers label each one and say plainly
 *  what the phone needs for it to work. */

import type { PairHost } from "./api";

/** Where the off-tailnet setup is explained (phone-access guide), and what a public console
 *  exposes and how to put an identity-aware proxy in front (exposing guide). */
export const PUBLIC_URL_GUIDE = "https://protolabsai.github.io/protoAgent/guides/phone-access#without-tailscale";
export const PUBLIC_CONSOLE_DOCS = "https://protolabsai.github.io/protoAgent/guides/exposing-protoagent#console-hostname";

/** The persistent Devices-panel warning while `network.public_console_url` is set (ADR 0121 D6). */
export const PUBLIC_CONSOLE_WARNING =
  "Anyone on the internet can reach this console's sign-in. Put Cloudflare Access (or another identity-aware proxy) in front of it.";

/** The declared public console URL from a server answer: a trimmed string, or "" (unset, or
 *  an older server that doesn't report it). */
export function publicConsoleUrl(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** What the address picker shows. */
export function reachLabel(h: Pick<PairHost, "kind" | "via" | "host">): string {
  if (h.kind === "public") return `Phone without Tailscale · ${h.host}`;
  if (h.kind === "lan") return `Wi-Fi · ${h.host}`;
  if (h.via === "serve") return `Tailnet HTTPS · ${h.host}`;
  if (h.via === "magicdns") return `Tailnet name · ${h.host}`;
  return `Tailnet · ${h.host}`;
}

/** The honest one-liner under the QR: what the PHONE needs for this address to work. */
export function reachRequirement(h: Pick<PairHost, "kind" | "via" | "secure" | "funnel">): string {
  if (h.kind === "lan") return "Same Wi-Fi only — the phone must be on this network.";
  if (h.kind === "public") {
    return "Works from any network over HTTPS through your public URL — no Tailscale needed on the phone.";
  }
  if (h.funnel) {
    return "Also public on the internet through Tailscale Funnel — anyone with the link reaches the sign-in. Turn Funnel off unless you meant that.";
  }
  const need = "the phone needs the Tailscale app, signed in to the same tailnet";
  if (h.secure) return `Works from any network over HTTPS — ${need}.`;
  return `Works from any network — ${need}.`;
}

/** Stable React key: a tailnet name can appear twice (https and http), so `host` isn't unique. */
export function reachKey(h: { host: string; origin?: string; url?: string }): string {
  return h.origin ?? h.url ?? h.host;
}

/** Wait for a restarting server: first see it go DOWN (or give up looking after `downMs`
 *  — a fast restart can come back between polls), then wait for it to come back UP.
 *  Resolves true when it's back, false on timeout. `probe` is `api.serverUp`. */
export async function waitForRestart(
  probe: () => Promise<boolean>,
  opts: { intervalMs?: number; downMs?: number; timeoutMs?: number; sleep?: (ms: number) => Promise<void> } = {},
): Promise<boolean> {
  const interval = opts.intervalMs ?? 500;
  const downMs = opts.downMs ?? 4000;
  const timeout = opts.timeoutMs ?? 60000;
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  let waited = 0;
  let sawDown = false;
  while (waited < timeout) {
    await sleep(interval);
    waited += interval;
    const up = await probe();
    if (!up) sawDown = true;
    else if (sawDown || waited >= downMs) return true;
  }
  return false;
}
