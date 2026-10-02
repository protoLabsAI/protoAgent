/**
 * Hide a trailing, still-empty markdown marker from IN-PROGRESS streamed text.
 *
 * Streamdown already repairs incomplete markdown while a message streams (`remend`:
 * `**bo` → `**bo**`, `[link` → a placeholder link). But it deliberately leaves a marker with
 * NOTHING after it alone — there is no content to close around — so the first delta of
 * `**protoAgent**`, which is just `**`, painted a literal `**` for a frame or two (launch-demo
 * flash). Likewise a bare `` ` `` / ```` ``` ````, `~~`, `_`, a lone `[`, and a list marker with
 * no item yet (`- ` → an empty bullet, or a setext underline turning the line above into a
 * heading for a frame).
 *
 * This pre-pass drops exactly that dangling tail. The next delta brings the content and the
 * marker reappears, now with something to format. It must ONLY run on text that is still
 * streaming: settled text is rendered verbatim.
 */
export function hideDanglingMarker(text: string): string {
  // Peel repeatedly: `- **` drops the `**`, which leaves an empty `- ` bullet to drop too.
  for (;;) {
    const next = peelOnce(text);
    if (next === text) return text;
    text = next;
  }
}

function peelOnce(text: string): string {
  if (!text) return text;
  const lastNl = text.lastIndexOf("\n");
  const head = text.slice(0, lastNl + 1);
  const line = text.slice(lastNl + 1);

  // Inside an open fenced code block the tail is code, not markdown: leave it alone. (The
  // last line opening a fence itself is handled below as a bare-marker run.)
  const fences = head.split("\n").filter((l) => /^\s{0,3}(```|~~~)/.test(l)).length;
  if (fences % 2 === 1) return text;

  // A line holding only a block marker with no content yet: a list bullet or number, a
  // heading `#`s, a quote `>`, or a run of `-` / `=` (an hr or a setext underline in progress).
  if (/^\s*(?:[-*+]|\d{1,9}[.)]|#{1,6}|>|-+|=+)\s*$/.test(line) && line.trim() !== "") return head;

  // An inline marker run at the very end with nothing after it, at a word boundary (start of
  // line, after whitespace or opening punctuation): `**`, `*`, `***`, `_`, `__`, `~~`, `` ` ``,
  // ```` ``` ````, `[`, `![`. A run glued to a word (`**bold**` closing) is real formatting.
  const m = /(^|[^\w*_~`\\[!])(!?\[|[*_~`]+)$/.exec(line);
  if (m) return head + line.slice(0, m.index + m[1].length).replace(/[ \t]+$/, "");
  return text;
}
