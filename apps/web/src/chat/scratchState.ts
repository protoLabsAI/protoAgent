// Per-session composer scratch state (Swap & Resume S3): the draft you were
// typing, queued steers, and your scroll position survive an agent switch or
// reload. sessionStorage by design — per-tab (two tabs don't fight over a
// draft) and gone when the tab closes (a draft is scratch, not a document).
// Keys are slug-namespaced like the chat store: sessions are per agent.

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
    return window.sessionStorage.getItem(key(kind, sessionId));
  } catch {
    return null;
  }
}

function write(kind: string, sessionId: string, value: string | null): void {
  try {
    if (value === null || value === "") window.sessionStorage.removeItem(key(kind, sessionId));
    else window.sessionStorage.setItem(key(kind, sessionId), value);
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

export function loadSteers(sessionId: string): { id: string; text: string }[] {
  const raw = read("steers", sessionId);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((s) => s && typeof s.id === "string" && typeof s.text === "string")
      : [];
  } catch {
    return [];
  }
}

export function saveSteers(sessionId: string, steers: { id: string; text: string }[]): void {
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
