/** `@<name>` autocomplete-token parsing for the composer (#3042).
 *
 *  Purely the AUTOCOMPLETE trigger — where the popover opens as you type — not where a
 *  mention routes. It opens for whatever `@`-token the caret is in, ANYWHERE in the
 *  message, exactly like `slashTokenAt` does for `/`: "hello team, @bob and @bill should
 *  pair on this" pops the picker at both `@bob` and `@bill`, so you get name completion
 *  in ordinary prose to the lead — not only in a leading `@bob @bill` run.
 *
 *  Routing is a separate, deliberately different rule (server `_parse_at_delegates`): a
 *  LEADING `@name` (run) addresses that participant directly; a mid-message `@name` is
 *  prose the lead reads and coordinates. Completion here just inserts the name at the
 *  caret — it never implies the mid-message mention will short-circuit to that delegate.
 */

/** The `@name` token the caret sits in, or `null`. Mirrors `slashTokenAt`'s shape
 *  (`query` filters the popover; `start`/`end` bound a caret-anchored replace that
 *  preserves surrounding text). The `@` must begin the token — at the message start or
 *  after whitespace — so an email address (`josh@protolabs.studio`) never triggers it. */
export function mentionTokenAt(
  text: string,
  caret: number,
): { query: string; start: number; end: number } | null {
  const pos = Math.max(0, Math.min(caret, text.length));
  // Walk back to the start of the token the caret is in.
  let start = pos;
  while (start > 0 && !/\s/.test(text[start - 1])) start -= 1;
  // The token must BEGIN with the sigil — otherwise the caret is in an ordinary word, or
  // in the domain half of an email where the `@` sits mid-token.
  if (text[start] !== "@") return null;
  // The token runs to the next whitespace at/after the caret, so completing mid-token
  // replaces the whole thing (no dangling tail).
  let end = pos;
  while (end < text.length && !/\s/.test(text[end])) end += 1;
  const query = text.slice(start + 1, pos);
  // `@` alone at the caret is an empty query — the "show everyone" state. Any typed query
  // must be name-shaped, or the caret is in something that isn't a mention.
  if (query && !/^[A-Za-z][\w.-]*$/.test(query)) return null;
  return { query, start, end };
}
