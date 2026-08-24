/** The leading mention run of a DRAFT, and the edits the cast chips make to it (#3049).
 *
 *  The chips compose the draft's leading run — the only position where a mention
 *  routes. Everything here is pure string→string so the behavior is testable without
 *  mounting the surface, and self-contained so it collides with nothing.
 */

/** Split a draft into its leading `@`-token run and the message body.
 *  Purely syntactic — the caller decides which names are real. */
export function leadingRun(draft: string): { names: string[]; body: string } {
  const names: string[] = [];
  let rest = draft;
  while (true) {
    const m = /^@(\S+)(?:\s+|$)/.exec(rest);
    if (!m) break;
    names.push(m[1]);
    rest = rest.slice(m[0].length);
  }
  return { names, body: rest };
}

/** Is `name` already addressed by the draft's leading run? (Chip active state.) */
export function runHas(draft: string, name: string): boolean {
  return leadingRun(draft).names.some((n) => n.toLowerCase() === name.toLowerCase());
}

/** Toggle `name` in the draft's leading run, preserving the message body.
 *
 *  Add appends to the run (written order = dispatch order); remove drops just that
 *  mention. The run is rebuilt at position 0 because that is the only place a mention
 *  routes — inserting at a caret inside the body would produce prose that LOOKS like an
 *  address and isn't.
 */
export function toggleMention(draft: string, name: string): string {
  const { names, body } = leadingRun(draft);
  const next = runHas(draft, name)
    ? names.filter((n) => n.toLowerCase() !== name.toLowerCase())
    : [...names, name];
  const run = next.map((n) => `@${n}`).join(" ");
  return run ? `${run} ${body}` : body;
}

/** The prefill for continuing an addressed conversation: the run that was just
 *  answered, ready for the next message. Empty when there is nothing to continue. */
export function continueRun(names: string[]): string {
  return names.length ? names.map((n) => `@${n}`).join(" ") + " " : "";
}
