// "Reply" on a Home card (ADR 0135, #5545): quote one entry into Home's composer so the
// operator can answer that entry specifically without retyping it. The reply is an ORDINARY
// Home message: it is written into Home's own draft and sent by the composer's normal path, so
// it never resumes or steers the session of the job that raised the entry.
//
// The draft lives in two places: sessionStorage (scratchState) and, while Home's slot is
// mounted, the composer's React state. `replyToHomeEntry` writes the first; the mounted
// composer picks the reply up through `useHomeReplyAdoption` and takes focus.

import { useEffect, type RefObject } from "react";

import { loadDraft, loadDraftContext, saveDraft, saveDraftContext } from "./scratchState";

/** Opening of every Home session id (`runtime/home_session.py: HOME_SESSION_PREFIX`). */
const HOME_SESSION_PREFIX = "chat-home-";
/** A quote is a pointer, not a copy: the hidden context names the entry in full. */
const QUOTE_MAX_CHARS = 600;

export function isHomeSessionId(sessionId: string | null | undefined): sessionId is string {
  return typeof sessionId === "string" && sessionId.startsWith(HOME_SESSION_PREFIX) && sessionId.length > HOME_SESSION_PREFIX.length;
}

export type HomeReply = {
  /** The visible markdown quote put in the composer. Ends with a blank line to type under. */
  quote: string;
  /** The agent-facing reference to the entry. Never shown in the editor or the sent bubble. */
  context: string;
};

/** A markdown blockquote of an entry's title and body, bounded in length. */
export function quoteEntry(title: string, body = ""): string {
  let text = [title.trim(), body.trim()].filter(Boolean).join("\n");
  if (text.length > QUOTE_MAX_CHARS) text = `${text.slice(0, QUOTE_MAX_CHARS).trimEnd()}…`;
  const quoted = text
    .split("\n")
    .map((line) => (line.trim() ? `> ${line}` : ">"))
    .join("\n");
  return `${quoted}\n\n`;
}

/** The hidden reference the agent receives with the reply. `subject` says which entry; the
 *  fixed tail says what a reply is, so the agent answers in Home instead of acting on the job. */
export function replyContext(subject: string): string {
  // The reference travels as an HTML comment; text from the entry must not close it early.
  const safe = subject.replace(/--+>/g, "").replace(/\s+/g, " ").trim();
  return `<!-- Replying to ${safe}. This is an ordinary Home message about that entry: answer it here; do not resume or steer the session that produced it. -->`;
}

/** The draft with `quote` on top. Text already being typed is kept below it, and a second
 *  click on the same card does not stack the quote twice. */
export function draftWithQuote(draft: string, quote: string): string {
  if (!draft.trim()) return quote;
  if (draft.includes(quote.trimEnd())) return draft;
  return `${quote}${draft}`;
}

type Listener = (sessionId: string, reply: HomeReply) => void;
const listeners = new Set<Listener>();

/** Write a reply to an entry into `sessionId`'s composer draft and hand it to the mounted
 *  composer, which shows it and takes focus. Nothing is sent. */
export function replyToHomeEntry(sessionId: string, reply: HomeReply): void {
  const draft = loadDraft(sessionId);
  // Context left by an earlier reply still in the draft is kept, so both entries stay named.
  const earlier = draft.trim() ? loadDraftContext(sessionId) : "";
  saveDraft(sessionId, draftWithQuote(draft, reply.quote));
  saveDraftContext(sessionId, earlier && !earlier.includes(reply.context) ? `${earlier}\n${reply.context}` : reply.context);
  for (const listener of [...listeners]) listener(sessionId, reply);
}

/** The composer side of Reply: adopt a reply aimed at this session into the live draft state
 *  and focus the textarea. Runs inside the click, so a phone opens its keyboard. */
export function useHomeReplyAdoption(
  sessionId: string,
  setDraft: (update: (draft: string) => string) => void,
  textareaRef: RefObject<HTMLTextAreaElement | null>,
): void {
  useEffect(() => {
    const listener: Listener = (target, reply) => {
      if (target !== sessionId) return;
      // From the composer's own state, not storage: the stored draft trails typing by a debounce.
      setDraft((draft) => draftWithQuote(draft, reply.quote));
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus();
      // Once the new value has rendered, put the caret under the quote.
      requestAnimationFrame(() => {
        if (document.activeElement !== textarea) return;
        const end = textarea.value.length;
        textarea.setSelectionRange(end, end);
        textarea.scrollTop = textarea.scrollHeight;
      });
    };
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);
}
