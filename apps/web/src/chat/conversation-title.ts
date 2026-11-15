// Pure title / meta helpers for the Conversations tab (docs/design/other-conversations.md,
// §3.4 item 2c and §9 C9). No React, no store, no DOM — the C9c card wires these into
// ConversationsPane / ConversationReader; here they are just string in, string out, so every
// case in the acceptance list is a plain unit test.

/** Longest "From <caller>:" prefix we will strip. A genuine caller prefix is short; anything
 *  longer is far more likely to be a real sentence that merely starts with "From …:", so we
 *  leave it alone (acceptance case g). */
const MAX_PREFIX_LEN = 60;

/** Escape a caller name so it is matched literally inside a RegExp — a name like "A.B (x)"
 *  must not have its "." or "(" read as metacharacters (acceptance case j). */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Uppercase the first character, leaving the rest untouched ("please check…" → "Please
 *  check…"). A non-letter first char is returned unchanged. */
function capitalizeFirst(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Drop a redundant "From <caller>:" (or "From <onBehalfOf>:") prefix from a conversation
 *  title (§3.4 item 2c). The caller already shows in the origin line, so a title the caller
 *  wrote as "From Claude Code (Josh's session): Josh's goal…" reads better as just "Josh's
 *  goal…". The match is deliberately strict so an unrelated "From …:" sentence survives:
 *
 *   - the name after "From " must be the `caller` or `onBehalfOf`, matched literally and
 *     case-insensitively, and must end on a word boundary — a space, a "(" opening a
 *     qualifier, or the colon itself (so "Claude Codex" is NOT stripped for "Claude Code");
 *   - anything between the name and the colon (e.g. " (Josh's session)") is allowed;
 *   - the colon must fall within the first {@link MAX_PREFIX_LEN} characters;
 *   - a title that is *only* the prefix keeps its full text (nothing useful to show).
 *
 *  The remaining text has its first letter capitalised. With no caller/onBehalfOf, no colon,
 *  or no match, the title is returned unchanged. */
export function displayTitle(
  title: string,
  { caller, onBehalfOf }: { caller?: string | null; onBehalfOf?: string | null } = {},
): string {
  const raw = title ?? "";
  const names = [caller, onBehalfOf].map((n) => (n ?? "").trim()).filter(Boolean);
  if (names.length === 0) return raw;

  const colon = raw.indexOf(":");
  if (colon < 0 || colon > MAX_PREFIX_LEN) return raw;

  for (const name of names) {
    // ^From <name>[ boundary ][ …qualifier… ]:  — see the doc comment above.
    const re = new RegExp(`^from\\s+${escapeRegExp(name)}(?=[\\s:(])[^:]*:\\s*`, "i");
    const m = re.exec(raw);
    if (!m) continue;
    const rest = raw.slice(m[0].length);
    if (!rest.trim()) return raw; // only the prefix — keep the whole title (case h)
    return capitalizeFirst(rest);
  }
  return raw;
}

/** Drop a single leading "From " (case-insensitive) from an origin label — "From Claude Code
 *  for Josh" → "Claude Code for Josh", "From another agent" → "another agent". A label that
 *  does not start with "From " (e.g. "Delegation from protoEngineer") is left whole. */
function withoutFrom(label: string): string {
  return label.replace(/^from\s+/i, "");
}

/** Parse the row's `last_updated` to a Date. A number is epoch seconds (relTime's convention);
 *  a string is an ISO instant. Null / empty / unparseable → null (the date part is omitted). */
function instant(value: number | string | null | undefined): Date | null {
  if (value == null || value === "") return null;
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date : null;
}

/** The muted meta line under a conversation row / thread header (§9 C9, criterion 9):
 *
 *   text    "Claude Code for Josh · 14 Nov, 12:29 · 14 messages"   (origin without "From ")
 *   tooltip "From Claude Code for Josh · 14 Nov at 12:29 · 14 messages"   (the long form)
 *
 *  The date is an absolute local "d Mon" + 24-hour "HH:mm" (en-GB), not a relative age. The
 *  message count is shown only above 1. A missing timestamp drops the date part; a missing
 *  origin drops the origin part. Returns `{ text, tooltip }` so the caller can hang the long
 *  form on a DS Tooltip over the short one. */
export function conversationMeta(
  originLabel: string | null | undefined,
  lastUpdated: number | string | null | undefined,
  turnCount: number | null | undefined,
): { text: string; tooltip: string } {
  const origin = (originLabel ?? "").trim();
  const date = instant(lastUpdated);
  const day = date ? date.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : "";
  const time = date ? date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "";
  const count = typeof turnCount === "number" && turnCount > 1 ? `${turnCount} messages` : "";
  return {
    text: [withoutFrom(origin), day && `${day}, ${time}`, count].filter(Boolean).join(" · "),
    tooltip: [origin, day && `${day} at ${time}`, count].filter(Boolean).join(" · "),
  };
}
