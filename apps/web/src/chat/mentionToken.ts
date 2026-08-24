/** `@<name>` token parsing for the composer (#3042) — the client twin of the server's
 *  `graph.mentions.parse_mention`.
 *
 *  The two MUST agree on one rule: only a **leading** mention addresses a turn. The
 *  dispatcher routes `@proto fix it` and does not route `ask @proto about it`, so offering
 *  autocomplete on that second `@` would suggest a target the message will never reach —
 *  the operator picks a name, sends, and the lead agent answers instead. Anchoring the
 *  popover to position 0 is what keeps the affordance honest.
 */

/** The `@name` token the caret sits in, or `null`.
 *
 *  Mirrors `slashTokenAt`'s shape (`query` filters the popover; `start`/`end` bound a
 *  caret-anchored replace that preserves any tail), with the leading-only rule above and
 *  the sigil-must-be-followed-immediately rule from the server parser — `@ me when it
 *  lands` is prose, not an address.
 */
export function mentionTokenAt(
  text: string,
  caret: number,
): { query: string; start: number; end: number } | null {
  if (text[0] !== "@") return null;
  const pos = Math.max(0, Math.min(caret, text.length));
  // The token runs from 0 to the next whitespace. A caret past that whitespace is in the
  // MESSAGE, not the address — the target is already chosen and the popover must close.
  let end = 0;
  while (end < text.length && !/\s/.test(text[end])) end += 1;
  if (pos > end) return null;
  const query = text.slice(1, pos);
  // A bare sigil addresses nobody, and a token with non-name characters isn't a name.
  if (query && !/^[A-Za-z][\w.-]*$/.test(query)) return null;
  return { query, start: 0, end };
}
