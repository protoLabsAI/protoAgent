// ADR 0118 D4 — the send-to-chat / openLink bridge GATES, enforced in the CONSOLE HOST and
// never in untrusted frame code. An artifact (or frame-component, S12) frame calls
// window.protoArtifact.send(text) / .openLink(url); the shell relays the request up to the
// host, and the host runs these gates before it ever starts a turn or opens a tab.
//
// This module is framework-free pure logic plus a tiny per-frame rate-limit registry. It
// returns typed verdicts and does NOT touch chat state, window.open or the DOM — S10b wires
// the verdicts into the chat send path and the openLink window. Keeping the trust decision
// here (not in the frame, which is model-authored) is the whole point of D4.

/** Max characters a send() may carry — the D4 length gate (mirrors the artifact ask cap). */
export const SEND_MAX_CHARS = 4000;
/** The D4 rate gate: at most one ACCEPTED send per this many ms, per frame. */
export const SEND_RATE_LIMIT_MS = 2000;
/** The window.open feature string every openLink the host opens must carry (D4). A frame can
 *  never reach the opener window or the referrer through it. */
export const OPEN_LINK_FEATURES = "noopener,noreferrer";
/** How much of the text the needs-confirm prompt previews before eliding. */
const CONFIRM_PREVIEW_CHARS = 80;

export type SendRejectReason = "empty" | "too-long" | "no-gesture" | "busy" | "rate-limited";

/** The outcome of a send() request, as judged by the host:
 *  - `ok`            — allowed; `text` is the normalized message to post as the user turn.
 *  - `needs-confirm` — the gesture API is missing on this runtime, so the host cannot trust a
 *                      silent activation; it must ask the user (`message`) before posting `text`.
 *  - `rejected`      — refused; `message` is shown IN the frame (e.g. "the agent is busy"). */
export type SendVerdict =
  | { status: "ok"; text: string }
  | { status: "needs-confirm"; text: string; message: string }
  | { status: "rejected"; reason: SendRejectReason; message: string };

export interface SendCheck {
  /** Identifies the originating frame, for the per-frame rate window. */
  frameId: string;
  /** The text the frame asked to send. */
  text: string;
  /** The host's OWN `navigator.userActivation`. `undefined`/`null` means the API is missing on
   *  this runtime — the host must confirm with the user rather than trust a silent gesture.
   *  (User Activation v2 propagates a child frame's activation to its ancestors, so a frame
   *  can't fake `isActive` by posting a message on its own.) */
  userActivation?: { isActive: boolean } | null;
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

/** A short, single-line preview of `text` for the needs-confirm prompt. */
function previewText(text: string): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length > CONFIRM_PREVIEW_CHARS ? oneLine.slice(0, CONFIRM_PREVIEW_CHARS) + "…" : oneLine;
}

/**
 * The pure core of the send gate. `lastSentAt` is the frame's last ACCEPTED send time (or
 * null if it has never sent), `now` the current time — both injected so this stays pure. The
 * caller owns the per-frame state; {@link createFrameBridge} supplies it.
 *
 * Gate order is deliberate: content validity (empty / too-long), then whether a send is
 * possible right now (busy, rate), then the gesture decision LAST — so needs-confirm only ever
 * fires for a send that would otherwise be allowed, and the host never asks the user to confirm
 * a message it would reject anyway.
 */
export function evaluateSend(check: SendCheck, lastSentAt: number | null, now: number): SendVerdict {
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
  const ua = check.userActivation;
  if (ua === undefined || ua === null) {
    // No User Activation API on this runtime — can't trust a silent gesture, so ask first.
    return { status: "needs-confirm", text, message: `Send "${previewText(text)}" to chat?` };
  }
  if (!ua.isActive) {
    return {
      status: "rejected",
      reason: "no-gesture",
      message: "A message can only be sent to chat from a click or key press.",
    };
  }
  return { status: "ok", text };
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

export interface FrameBridge {
  /** Judge a send() request. On an `ok` verdict the frame's rate window is advanced (the send
   *  is approved and goes out now); `needs-confirm` and `rejected` leave it untouched. Call
   *  once per attempt. */
  checkSend(check: SendCheck): SendVerdict;
  /** Advance `frameId`'s rate window because a send actually went out — used for the
   *  needs-confirm path, where the send happens only after the user confirms (so `checkSend`
   *  could not have advanced it). */
  noteSend(frameId: string): void;
  /** Judge an openLink() request (stateless). */
  checkOpenLink(check: OpenLinkCheck): OpenLinkVerdict;
}

/**
 * A stateful host bridge holding the per-frame rate windows. `now` is injectable for tests;
 * it defaults to the wall clock.
 */
export function createFrameBridge(opts: { now?: () => number } = {}): FrameBridge {
  const now = opts.now ?? (() => Date.now());
  // frameId → last accepted send time. Scoped to one host; a frame that never sends never
  // appears here, so this can't grow without a send.
  const lastSent = new Map<string, number>();

  return {
    checkSend(check) {
      const verdict = evaluateSend(check, lastSent.get(check.frameId) ?? null, now());
      if (verdict.status === "ok") lastSent.set(check.frameId, now());
      return verdict;
    },
    noteSend(frameId) {
      lastSent.set(frameId, now());
    },
    checkOpenLink,
  };
}
