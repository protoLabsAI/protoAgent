/** Agent pairing (ADR 0113) — the pure bits the Devices panel and the Fleet panel share.
 *
 * Two halves of one handshake live in two panels on two machines: the REMOTE mints a
 * typeable code (Settings ▸ Devices ▸ Pair an agent), the HUB types it into Pair… on its
 * Fleet panel and redeems it server-side. Everything here is pure so the rules (code shape,
 * countdown, cancel scoping, the auth badge, and which A2A URL a delegate may be written
 * with) are unit-tested without rendering either panel.
 *
 * Not to be confused with `lib/pairing.ts`: that is the PHONE claim half (a `#pair=` URL
 * fragment redeemed before React mounts). An agent code is never claimed by a browser — the
 * hub's server claims it (`POST /api/fleet/remotes/pair`), so no token ever reaches this
 * console. */

import type { FleetAgent, RemoteAuth } from "./types";

/** The two code kinds `POST /api/pairing/start` mints (ADR 0113 D2). */
export type PairKind = "device" | "agent";

/** An agent code is 10 Crockford base32 characters shown as `XXXXX-XXXXX`. */
export const AGENT_CODE_LENGTH = 10;

/** The code as typed, reduced to its significant characters: upper-cased, with dashes,
 *  spaces and anything else that isn't a letter or digit dropped, capped at 10.
 *
 *  Deliberately NOT applying the Crockford aliases (O→0, I/L→1) here — the server's claim
 *  normalizes those (D2), and rewriting a letter the operator just typed into a digit reads
 *  as the field fighting them. What the field must do is tolerate the separators people
 *  actually paste: `abcde-fghij`, `ABCDE FGHIJ`, `abcdefghij`. */
export function normalizeAgentCode(raw: string): string {
  return raw
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, "")
    .slice(0, AGENT_CODE_LENGTH);
}

/** Format for display as the operator types: `ABCDE-FGHIJ`. The dash appears only once a
 *  sixth character exists, so backspacing over it never leaves a dangling separator. */
export function formatAgentCode(raw: string): string {
  const n = normalizeAgentCode(raw);
  return n.length > 5 ? `${n.slice(0, 5)}-${n.slice(5)}` : n;
}

/** A full code has been entered — the Pair button's gate (the server is authoritative). */
export function isCompleteAgentCode(raw: string): boolean {
  return normalizeAgentCode(raw).length === AGENT_CODE_LENGTH;
}

/** Whole seconds until `expiresAt` (epoch SECONDS, the server's clock), never negative. */
export function secondsLeft(expiresAt: number, nowMs: number = Date.now()): number {
  return Math.max(0, Math.round(expiresAt - nowMs / 1000));
}

/** `m:ss` — an agent code lives 5 minutes, and "287s" makes the operator do arithmetic. */
export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The body `POST /api/pairing/cancel` gets. ALWAYS scoped to the dialog's own kind:
 *  an unscoped cancel drops every pending code, so closing the phone QR would silently kill
 *  an agent code the operator is halfway through typing on another machine (and vice
 *  versa). The no-body form is only for pre-ADR-0113 consoles. */
export function cancelBody(kind: PairKind): { kind: PairKind } {
  return { kind };
}

/** Reachable base URLs, tailnet first — the address that works from any network the other
 *  hub is on beats one that only works on this Wi-Fi. Stable within a kind. */
export function tailnetFirst<T extends { kind: string }>(hosts: readonly T[]): T[] {
  return hosts
    .map((h, i) => ({ h, i }))
    .sort((a, b) => (a.h.kind === "tailnet" ? 0 : 1) - (b.h.kind === "tailnet" ? 0 : 1) || a.i - b.i)
    .map(({ h }) => h);
}

/** Label for a reachable-address kind. */
export function hostKindLabel(kind: string): string {
  return kind === "tailnet" ? "Tailnet" : "LAN";
}

/** What a remote row shows for its `auth` verdict (ADR 0113 D5), or null for nothing.
 *
 *  - `rejected` — the remote refused the stored token (revoked, rotated): a WARNING badge,
 *    and the row offers Re-pair. Without it the row showed a green dot that 401s on click.
 *  - `none` — no token stored (added by discovery or tokenless by URL): "not paired".
 *    Neutral, not a warning — an open remote on a trusted network is a legitimate setup.
 *  - `ok` — a subtle success mark; the absence of trouble shouldn't be louder than trouble.
 *  - `open` — the remote answers without any token (so the stored one is unverifiable).
 *  - `unknown` / absent (an older hub, or the first probe hasn't landed) — nothing. Guessing
 *    would be worse than silence.
 */
export function remoteAuthBadge(
  auth: RemoteAuth | undefined,
): { status: "warning" | "neutral" | "success"; label: string; title: string; repair: boolean } | null {
  switch (auth) {
    case "rejected":
      return {
        status: "warning",
        label: "token rejected — re-pair",
        title: "The remote refused this hub's token (revoked or rotated there). Re-pair with a fresh code.",
        repair: true,
      };
    case "none":
      return {
        status: "neutral",
        label: "not paired",
        title: "No token stored for this remote. Pair it to reach a token-gated agent.",
        repair: true,
      };
    case "ok":
      return { status: "success", label: "paired", title: "The remote accepts this hub's paired token.", repair: false };
    case "open":
      // The remote answers without ANY token, so the stored one can't be verified — and isn't
      // needed. Neutral: an open instance on a trusted network is a choice, not a fault.
      return {
        status: "neutral",
        label: "open — no token needed",
        title: "The remote answers without a token, so the stored one can't be checked (and isn't needed).",
        repair: false,
      };
    default:
      return null;
  }
}

/** The result of choosing the URL an "Add as delegate" gesture may write. */
export type DelegateLink = { url: string; reason?: undefined } | { url: null; reason: string };

/** Which A2A URL "Add as a delegate" may write into the FOCUSED agent's delegate registry.
 *
 *  `/api/fleet` always comes from the HUB, so every member's `a2a` is a URL that resolves ON
 *  THE HUB'S BOX: a local member's own `http://127.0.0.1:<port>/a2a`, and a remote member's
 *  hub-loopback proxy `http://127.0.0.1:<hub>/agents/<id>/a2a` (ADR 0113 D4) — which works
 *  only for a caller that shares the hub's loopback AND holds the fleet service token. But
 *  `createDelegate` posts to the FOCUSED agent. Focused on the hub or a local member, that
 *  caller is on the hub's box with the fleet token, so the URL is right. Focused on a REMOTE
 *  member, the write lands on another machine, where `127.0.0.1` is that machine itself —
 *  the delegate would dial the wrong process, or nothing.
 *
 *  So from a remote's window the gesture is refused with a reason rather than rewritten to
 *  the target's real URL: a direct `<url>/a2a` delegate carries no token and 401s against
 *  any paired (token-gated) agent, which is the exact failure pairing exists to remove. The
 *  remote's own hub (or Settings ▸ Delegates with a token) is where that link belongs.
 *
 *  An unknown focus (a slug the roster doesn't list yet) is treated like a remote: refusing
 *  a click is recoverable, a wrong URL in someone's config is not. */
export function delegateLinkFor(agents: readonly FleetAgent[], slug: string, target: FleetAgent): DelegateLink {
  if (!target.a2a) return { url: null, reason: "This agent has no A2A endpoint yet." };
  const refusal = hubUrlRefusal(agents, slug);
  return refusal ? { url: null, reason: refusal } : { url: target.a2a };
}

/** Why the FOCUSED agent can't be given a hub-box URL (see `delegateLinkFor`), or null when
 *  it can — the focused agent is the hub itself or a local member. Also gates the Pair
 *  dialog's "also add as a delegate" option, which writes the same kind of URL. */
export function hubUrlRefusal(agents: readonly FleetAgent[], slug: string): string | null {
  const focused = agents.find((a) => (a.host ? "host" : a.id) === slug);
  if (!focused && slug !== "host") {
    return "The agent this window is on isn't in the fleet list — reload and try again.";
  }
  if (focused?.remote) {
    return (
      "This window is a remote agent on another machine, where the hub's local address doesn't reach. " +
      "Add the delegate from that agent's own hub, or in its Settings ▸ Delegates with a token."
    );
  }
  return null;
}

// ── D10: a credential never crosses a plaintext network without an explicit opt-in ──────

/** The warning shown beside the opt-in (ADR 0113 D10). One string, so the Pair dialog and the
 *  add/edit-remote form can't drift apart. */
export const INSECURE_WARNING =
  "This sends the pairing code and the token unencrypted on your network. Prefer the agent's tailnet address or https.";
export const INSECURE_OPT_IN = "I trust this network — send it unencrypted";

function ipv4Octets(host: string): number[] | null {
  const m = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!m) return null;
  const o = m.slice(1).map(Number);
  return o.every((n) => n <= 255) ? o : null;
}

/** The eight 16-bit groups of an IPv6 literal (`::` expanded), or null when it isn't one. */
function ipv6Groups(host: string): number[] | null {
  if (!host.includes(":") || !/^[0-9a-f:.]+$/.test(host)) return null;
  const halves = host.split("::");
  if (halves.length > 2) return null;
  const part = (h: string) => (h ? h.split(":") : []);
  const head = part(halves[0]);
  const tail = halves.length === 2 ? part(halves[1]) : [];
  // An embedded IPv4 tail (`::ffff:1.2.3.4`) counts as two groups.
  const expand = (xs: string[]) =>
    xs.flatMap((x) => {
      const v4 = ipv4Octets(x);
      return v4 ? [(v4[0] << 8) | v4[1], (v4[2] << 8) | v4[3]] : [parseInt(x, 16)];
    });
  const h = expand(head);
  const t = expand(tail);
  const fill = 8 - h.length - t.length;
  if (halves.length === 1 ? fill !== 0 : fill < 0) return null;
  const groups = [...h, ...Array(Math.max(0, fill)).fill(0), ...t];
  return groups.length === 8 && groups.every((g) => Number.isInteger(g) && g >= 0 && g <= 0xffff) ? groups : null;
}

/** How a base URL's transport protects a credential — the browser half of the hub's rule
 *  (ADR 0113 D10, `graph/fleet/supervisor.py` `_cleartext_host`):
 *
 *  - `secure` — `https://`; or plain http to a loopback address (127.0.0.0/8, `::1`) or a
 *    tailnet one (100.64.0.0/10, Tailscale's IPv6 ULA `fd7a:115c:a1e0::/48` — WireGuard
 *    underneath), or a MagicDNS `*.ts.net` name. `localhost` counts too: the hub resolves it
 *    to loopback.
 *  - `insecure` — plain `http://` to any other LITERAL address: the hub refuses it without
 *    `allow_insecure`, so the console asks up front.
 *  - `unknown` — plain `http://` to some other NAME. The hub judges a name by the addresses
 *    it resolves to at that moment (and an unresolvable one as cleartext), which a browser
 *    can't know; asking up front would nag for names that resolve to a tailnet. The hub's 400
 *    reveals the opt-in instead.
 *  - `invalid` — not an http(s) URL at all (the submit gates on that separately).
 */
export function transportSecurity(raw: string): "secure" | "insecure" | "unknown" | "invalid" {
  let u: URL;
  try {
    u = new URL(raw.trim());
  } catch {
    return "invalid";
  }
  if (u.protocol === "https:") return "secure";
  if (u.protocol !== "http:") return "invalid";
  const host = u.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (host === "localhost" || host.endsWith(".ts.net")) return "secure";
  const v4 = ipv4Octets(host);
  if (v4) {
    if (v4[0] === 127) return "secure";
    if (v4[0] === 100 && v4[1] >= 64 && v4[1] <= 127) return "secure"; // 100.64.0.0/10
    return "insecure";
  }
  const v6 = ipv6Groups(host);
  if (v6) {
    if (v6.slice(0, 7).every((g) => g === 0) && v6[7] === 1) return "secure"; // ::1
    if (v6[0] === 0xfd7a && v6[1] === 0x115c && v6[2] === 0xa1e0) return "secure"; // tailnet ULA
    return "insecure";
  }
  return "unknown";
}

/** Whether to ask for the D10 opt-in before sending a credential to `url`. */
export function needsInsecureOptIn(url: string): boolean {
  return transportSecurity(url) === "insecure";
}

/** Is this error the hub's D10 refusal? It's a 400 whose detail names the plaintext problem
 *  (or the opt-in flag) — the case of an http NAME that resolved to a LAN address, which the
 *  console couldn't classify up front. Matched on the words, not an exact string, so a
 *  rephrased server message still reveals the checkbox. */
export function isInsecureRefusal(err: unknown): boolean {
  const status = (err as { status?: number } | null)?.status;
  const msg = err instanceof Error ? err.message : String(err ?? "");
  return (status === undefined || status === 400) && /allow_insecure|insecure|unencrypted|plain(?:text)? http|cleartext/i.test(msg);
}
