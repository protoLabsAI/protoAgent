// ADR 0118 D4 (amended by S19 / #4137) — the send-to-chat / openLink bridge GATES, enforced in
// the CONSOLE HOST and never in untrusted frame code. An artifact (or frame-component, S12) frame
// calls window.protoArtifact.send(text) / .openLink(url); the shell relays the request up to the
// host, and the host runs these gates before it ever starts a turn or opens a tab.
//
// "The frame proposes; the user decides." A send() is a PROPOSAL: unless the operator has chosen
// "Always allow" for that artifact (or component kind), it never posts on its own — the host shows
// an inline confirm chip in CONSOLE DOM, which the frame cannot click. The earlier gesture gate
// (navigator.userActivation + focus-in-frame) is gone: an artifact satisfied it by calling
// .focus() on itself after any console click (#4137). Trusted sources skip the chip but still pass
// the content, busy and rate gates.
//
// This module is framework-free logic plus a tiny per-frame rate registry and the per-browser
// trust store. It returns typed verdicts and does NOT touch chat state, window.open or the DOM —
// the hosts wire the verdicts into the chat send path and the confirm chip.

/** Max characters a send() may carry — the D4 length gate (mirrors the artifact ask cap). */
export const SEND_MAX_CHARS = 4000;
/** The D4 rate gate: at most one ACCEPTED send per this many ms, per frame. */
export const SEND_RATE_LIMIT_MS = 2000;
/** The proposal rate gate: at most one send() PROPOSAL shown per this many ms, per frame. A
 *  frame that polls send() can't churn the confirm chip (or flood its own error path). */
export const PROPOSAL_RATE_LIMIT_MS = 1000;
/** The window.open feature string every openLink the host opens must carry (D4). A frame can
 *  never reach the opener window or the referrer through it. */
export const OPEN_LINK_FEATURES = "noopener,noreferrer";
/** localStorage key holding the trusted send sources ("Always allow from this artifact"). */
export const SEND_TRUST_STORAGE_KEY = "protoagent:frame-send-trust:v1";

export type SendRejectReason = "empty" | "too-long" | "busy" | "rate-limited";

/** The outcome of a send() proposal, as judged by the host:
 *  - `ok`            — the source is trusted ("Always allow"); `text` is the normalized message to
 *                      post as the user turn now. The frame's send-rate window has been advanced.
 *  - `needs-confirm` — show the confirm chip; post `text` only when the operator clicks Send.
 *  - `rejected`      — refused; `message` goes back to the frame (e.g. "the agent is busy"). */
export type SendVerdict =
  | { status: "ok"; text: string }
  | { status: "needs-confirm"; text: string }
  | { status: "rejected"; reason: SendRejectReason; message: string };

export interface SendCheck {
  /** Identifies the originating frame OCCURRENCE, for the per-frame rate windows. */
  frameId: string;
  /** Identifies the trust SOURCE — `artifact:<id>` (every version of one artifact) or
   *  `component:<plugin>:<kind>`. Derived by the host from its own props, never from the frame's
   *  message. Omitted ⇒ never trusted (always needs-confirm). */
  trustKey?: string;
  /** The text the frame asked to send. */
  text: string;
  /** Injected predicate — true when the TARGET session already has a turn running. The bridge
   *  owns no session state, so the host answers this. Omitted ⇒ treated as idle. */
  isBusy?: () => boolean;
}

export type OpenLinkRejectReason = "invalid-url" | "not-https" | "origin-not-allowed";

/** The outcome of an openLink() request. On `ok`, S10b opens `url` with `features`. */
export type OpenLinkVerdict =
  | { status: "ok"; url: string; features: string }
  | { status: "rejected"; reason: OpenLinkRejectReason; message: string };

export interface OpenLinkCheck {
  url: string;
  /** Operator allowlist of origins (`artifact.open_link_origins`). Empty/absent ⇒ ANY https
   *  origin is allowed; a non-empty list narrows it to exactly those origins. */
  allowOrigins?: readonly string[];
}

/**
 * The pure core of the send gate. `lastSentAt` is the frame's last ACCEPTED send time (or null if
 * it has never sent), `now` the current time, `trusted` whether the operator has chosen "Always
 * allow" for this source — all injected so this stays pure. The proposal rate window is the
 * stateful bridge's concern ({@link createFrameBridge}).
 *
 * Gate order: content validity (empty / too-long), then whether a send is possible right now
 * (busy, rate), then trust LAST — so the chip only ever asks about a message that would otherwise
 * go out, and a trusted source is held to exactly the same content/busy/rate gates.
 */
export function evaluateSend(check: SendCheck, lastSentAt: number | null, now: number, trusted = false): SendVerdict {
  const text = String(check.text ?? "").trim();

  if (text.length < 1) {
    return { status: "rejected", reason: "empty", message: "Nothing to send." };
  }
  if (text.length > SEND_MAX_CHARS) {
    return {
      status: "rejected",
      reason: "too-long",
      message: `A message to chat must be ${SEND_MAX_CHARS} characters or fewer.`,
    };
  }
  if (check.isBusy?.()) {
    // Exact wording per D4 — the frame shows this string to the user.
    return { status: "rejected", reason: "busy", message: "the agent is busy" };
  }
  if (lastSentAt !== null && now - lastSentAt < SEND_RATE_LIMIT_MS) {
    return {
      status: "rejected",
      reason: "rate-limited",
      message: "Only one message can be sent to chat every 2 seconds.",
    };
  }
  return trusted ? { status: "ok", text } : { status: "needs-confirm", text };
}

/**
 * The pure openLink gate: https only, narrowed by an optional operator origin allowlist, and
 * carrying the mandatory {@link OPEN_LINK_FEATURES}. Stateless — no per-frame window to track.
 */
export function checkOpenLink(check: OpenLinkCheck): OpenLinkVerdict {
  let parsed: URL;
  try {
    parsed = new URL(String(check.url ?? ""));
  } catch {
    return { status: "rejected", reason: "invalid-url", message: "That is not a valid URL." };
  }
  if (parsed.protocol !== "https:") {
    return { status: "rejected", reason: "not-https", message: "Only https links can be opened." };
  }
  const allow = normalizeOrigins(check.allowOrigins);
  if (allow.size > 0 && !allow.has(parsed.origin)) {
    return {
      status: "rejected",
      reason: "origin-not-allowed",
      message: "That link's origin is not in the allowed list.",
    };
  }
  return { status: "ok", url: parsed.href, features: OPEN_LINK_FEATURES };
}

/** Normalize an allowlist to a set of origins. Entries may be full origins
 *  ("https://example.com"), URLs with a path, or a bare host ("example.com", assumed https).
 *  Blank or unparseable entries are dropped, so a stray empty line can't widen or break it. */
function normalizeOrigins(entries: readonly string[] | undefined): Set<string> {
  const out = new Set<string>();
  if (!entries) return out;
  for (const raw of entries) {
    const entry = String(raw ?? "").trim();
    if (!entry) continue;
    const origin = toOrigin(entry);
    if (origin) out.add(origin);
  }
  return out;
}

function toOrigin(entry: string): string | null {
  try {
    return new URL(entry).origin;
  } catch {
    // No scheme (a bare host) — assume https, the only scheme openLink allows.
    try {
      return new URL("https://" + entry).origin;
    } catch {
      return null;
    }
  }
}

/** Where "Always allow" lives: a set of trust keys, per browser. */
export interface SendTrustStore {
  isTrusted(key: string): boolean;
  trust(key: string): void;
}

type KeyValueStorage = Pick<Storage, "getItem" | "setItem">;

function defaultStorage(): KeyValueStorage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null; // storage blocked (privacy mode, sandboxed host) — session-only trust
  }
}

/**
 * A trust store persisted in `storage` (localStorage by default) under
 * {@link SEND_TRUST_STORAGE_KEY}. Every storage access is try/catch-wrapped: when storage is
 * missing, blocked, full or holds garbage, the store degrades to an in-memory set, so "Always
 * allow" still holds for this session and nothing throws into the host.
 */
export function createSendTrustStore(storage: KeyValueStorage | null = defaultStorage()): SendTrustStore {
  const session = new Set<string>();
  function load(): Set<string> {
    const out = new Set(session);
    try {
      const raw = storage?.getItem(SEND_TRUST_STORAGE_KEY);
      const parsed: unknown = raw ? JSON.parse(raw) : [];
      if (Array.isArray(parsed)) for (const k of parsed) if (typeof k === "string" && k) out.add(k);
    } catch {
      /* unreadable / corrupt — fall back to the session set */
    }
    return out;
  }
  return {
    isTrusted(key) {
      return !!key && load().has(key);
    },
    trust(key) {
      if (!key) return;
      session.add(key);
      try {
        const all = load();
        all.add(key);
        storage?.setItem(SEND_TRUST_STORAGE_KEY, JSON.stringify([...all]));
      } catch {
        /* quota / blocked — the session set still holds it */
      }
    },
  };
}

export interface FrameBridge {
  /** Judge a send() PROPOSAL. `ok` (trusted source) advances the frame's send-rate window — the
   *  host posts now. `needs-confirm` advances only the proposal-rate window — the host shows the
   *  chip and calls {@link FrameBridge.noteSend} if the operator clicks Send. Call once per
   *  attempt. A proposal inside the proposal window is `rejected` / `rate-limited`. */
  checkSend(check: SendCheck): SendVerdict;
  /** Advance `frameId`'s send-rate window because a send actually went out — the chip path,
   *  where the send happens only after the operator confirms. */
  noteSend(frameId: string): void;
  /** Whether the operator chose "Always allow" for this source. */
  isTrusted(trustKey: string): boolean;
  /** Record "Always allow" for this source (persisted per browser where storage allows). */
  trust(trustKey: string): void;
  /** Judge an openLink() request (stateless). */
  checkOpenLink(check: OpenLinkCheck): OpenLinkVerdict;
}

/**
 * A stateful host bridge holding the per-frame rate windows and the trust store. `now` and
 * `trust` are injectable for tests; they default to the wall clock and a localStorage store.
 */
export function createFrameBridge(opts: { now?: () => number; trust?: SendTrustStore } = {}): FrameBridge {
  const now = opts.now ?? (() => Date.now());
  const trustStore = opts.trust ?? createSendTrustStore();
  // frameId → last accepted send time / last shown proposal time. Scoped to one host; a frame
  // that never sends never appears here, so these can't grow without a send.
  const lastSent = new Map<string, number>();
  const lastProposed = new Map<string, number>();

  return {
    checkSend(check) {
      const t = now();
      const trusted = !!check.trustKey && trustStore.isTrusted(check.trustKey);
      const verdict = evaluateSend(check, lastSent.get(check.frameId) ?? null, t, trusted);
      if (verdict.status === "rejected") return verdict;
      const lastP = lastProposed.get(check.frameId);
      if (lastP !== undefined && t - lastP < PROPOSAL_RATE_LIMIT_MS) {
        return {
          status: "rejected",
          reason: "rate-limited",
          message: "This frame is sending to chat too often — try again in a moment.",
        };
      }
      lastProposed.set(check.frameId, t);
      if (verdict.status === "ok") lastSent.set(check.frameId, t);
      return verdict;
    },
    noteSend(frameId) {
      lastSent.set(frameId, now());
    },
    isTrusted: (key) => trustStore.isTrusted(key),
    trust: (key) => trustStore.trust(key),
    checkOpenLink,
  };
}
