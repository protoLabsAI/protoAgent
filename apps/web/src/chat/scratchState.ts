// Per-session composer scratch state (Swap & Resume S3): the draft you were
// typing, queued steers, and your scroll position survive an agent switch or
// reload. sessionStorage by design — per-tab (two tabs don't fight over a
// draft) and gone when the tab closes (a draft is scratch, not a document).
// Keys are slug-namespaced like the chat store: sessions are per agent.

import type { QueuedSteer } from "../lib/types";
import { readKey, removeKey, writeKey } from "../lib/storage";

const SLUG = (() => {
  try {
    const m = window.location.pathname.match(/\/agent\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : "host";
  } catch {
    return "host";
  }
})();

function key(kind: string, sessionId: string): string {
  return `protoagent.chat.${kind}:${SLUG}:${sessionId}`;
}

function read(kind: string, sessionId: string): string | null {
  try {
    return readKey("session", key(kind, sessionId));
  } catch {
    return null;
  }
}

function write(kind: string, sessionId: string, value: string | null): void {
  try {
    if (value === null || value === "") removeKey("session", key(kind, sessionId));
    else writeKey("session", key(kind, sessionId), value);
  } catch {
    /* hardened contexts: scratch state is best-effort */
  }
}

export function loadDraft(sessionId: string): string {
  return read("draft", sessionId) ?? "";
}

export function saveDraft(sessionId: string, draft: string): void {
  write("draft", sessionId, draft);
}

/** Hidden continuation context survives composer edits and reloads with the draft. */
export function saveDraftContext(sessionId: string, context: string): void {
  write("draft-context", sessionId, context);
}

export function loadDraftContext(sessionId: string): string {
  return read("draft-context", sessionId) ?? "";
}

/** A hidden context line that belongs to a quote in the visible draft ("Reply" on a Home
 *  card). The line is sent only while its quote is still in the draft. */
export type QuotedDraftContext = { quote: string; context: string };

export function loadQuotedDraftContexts(sessionId: string): QuotedDraftContext[] {
  const raw = read("draft-quotes", sessionId);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((q) => q && typeof q.quote === "string" && typeof q.context === "string")
      : [];
  } catch {
    return [];
  }
}

export function saveQuotedDraftContexts(sessionId: string, quoted: QuotedDraftContext[]): void {
  write("draft-quotes", sessionId, quoted.length ? JSON.stringify(quoted) : null);
}

/** Build the agent payload without putting machine notes in the editor or display text.
 *  Keep context until the send succeeds so a failed send can be retried. */
export function draftPayload(sessionId: string, text: string): string {
  const context = loadDraftContext(sessionId);
  return context ? `${text}\n\n${context}` : text;
}

export function loadSteers(sessionId: string): QueuedSteer[] {
  const raw = read("steers", sessionId);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed
          .filter((s) => s && typeof s.id === "string" && typeof s.text === "string")
          // An interjection keeps the server turn it was sent to, so a reload can still
          // tell whether that turn is over (ChatSessionSlot's server-turn reconcile).
          .map((s) => ({
            id: s.id,
            text: s.text,
            ...(typeof s.draftText === "string" ? { draftText: s.draftText } : {}),
            ...(typeof s.sendAs === "string" ? { sendAs: s.sendAs } : {}),
            ...(Array.isArray(s.images)
              ? { images: s.images.filter((image: Record<string, unknown>) =>
                  image && typeof image.b64 === "string" && typeof image.mime === "string" && typeof image.name === "string",
                ) }
              : {}),
            ...(typeof s.serverTaskId === "string" && s.serverTaskId ? { serverTaskId: s.serverTaskId } : {}),
            // A reload is itself a lost answer: an item whose POST was in flight comes back
            // unconfirmed, so the reconcile can't read its absence as "the agent read it".
            ...(s.unconfirmed === true ? { unconfirmed: true } : {}),
          }))
      : [];
  } catch {
    return [];
  }
}

export function saveSteers(sessionId: string, steers: QueuedSteer[]): void {
  write("steers", sessionId, steers.length ? JSON.stringify(steers) : null);
}

/** Scroll memory: a saved offset means "the operator had scrolled back"; no key
 * means pinned-to-bottom (the Conversation's default). */
export function loadScroll(sessionId: string): number | null {
  const raw = read("scroll", sessionId);
  const n = raw === null ? NaN : Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

export function saveScroll(sessionId: string, top: number | null): void {
  write("scroll", sessionId, top === null ? null : String(Math.round(top)));
}
