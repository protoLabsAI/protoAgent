/** `@<name>` token parsing for the composer (#3042) — the client twin of the server's
 *  `graph.mentions.parse_mention` / `server.chat._parse_at_delegates`.
 *
 *  The two MUST agree on where a mention can route. The dispatcher routes a **leading
 *  run** of mentions — `@proto @reviewer what do you think?` fans out to both — and a
 *  mid-message `@` is prose the addressees will read, not a routing instruction. So the
 *  popover opens for the token the caret is in only while every token BEFORE it is
 *  itself an `@`-mention: offering a completion past the end of the run would suggest a
 *  target the message never reaches — the operator picks a name, sends, and the lead
 *  agent answers instead.
 */

/** The `@name` token the caret sits in, or `null`.
 *
 *  Mirrors `slashTokenAt`'s shape (`query` filters the popover; `start`/`end` bound a
 *  caret-anchored replace that preserves any tail). The sigil must be followed
 *  immediately by the name — `@ me when it lands` is prose, not an address.
 */
export function mentionTokenAt(
  text: string,
  caret: number,
): { query: string; start: number; end: number } | null {
  if (text[0] !== "@") return null;
  const pos = Math.max(0, Math.min(caret, text.length));
  let i = 0;
  while (i < text.length) {
    // Every token up to (and including) the caret's must be an `@`-token — the first
    // non-mention token ends the run, and everything after it is the message.
    if (text[i] !== "@") return null;
    let end = i;
    while (end < text.length && !/\s/.test(text[end])) end += 1;
    if (pos < i) return null; // caret sits in the whitespace before this token
    if (pos <= end) {
      const query = text.slice(i + 1, pos);
      // A token with non-name characters isn't a name (and `@` alone at the caret is
      // an empty query — the "show everyone" state).
      if (query && !/^[A-Za-z][\w.-]*$/.test(query)) return null;
      return { query, start: i, end };
    }
    // Walk over the whitespace to the next token; end-of-text means the caret was
    // clamped into the final token above, so this only advances.
    let j = end;
    while (j < text.length && /\s/.test(text[j])) j += 1;
    if (j === end) return null;
    i = j;
  }
  return null;
}
