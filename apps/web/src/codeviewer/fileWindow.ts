// The File tab's window over a big file (ADR 0112). GET /api/fs/file caps a response at
// 20,000 lines, so a deep target (a code-ref at line 30,000) is fetched as a WINDOW around it,
// and the operator pages from there. Pure — every edge is unit-tested (fileWindow.test.ts).

/** Lines per request — the server's per-response cap. */
export const WINDOW_SIZE = 20_000;
/** Context kept above the target line when a window has to be cut around it. */
export const WINDOW_BEFORE = 5_000;

export type LineRange = { start: number; end: number };

/** The window to fetch so `line` is inside it, clamped to [1, lineCount]. When the file's
 *  tail is within reach — sliding the window down costs less than half the context above
 *  the target — the window ends AT EOF instead of stopping a few lines short of it (line
 *  30,000 of 45,000 → 25,001–45,000, not 25,000–44,999 with the last line silently gone). */
export function windowFor(line: number, lineCount: number): LineRange {
  const lc = Math.max(1, Math.floor(lineCount));
  if (lc <= WINDOW_SIZE) return { start: 1, end: lc };
  const target = Math.min(Math.max(1, Math.floor(line)), lc);
  let start = Math.max(1, target - WINDOW_BEFORE);
  const eofStart = lc - WINDOW_SIZE + 1;
  if (eofStart > start && eofStart - start <= WINDOW_BEFORE / 2) start = eofStart;
  start = Math.min(start, eofStart);
  return { start, end: Math.min(lc, start + WINDOW_SIZE - 1) };
}

/** The windows before / after the one on screen, or null at either edge of the file. */
export function pageBefore(shown: LineRange): LineRange | null {
  if (shown.start <= 1) return null;
  return { start: Math.max(1, shown.start - WINDOW_SIZE), end: shown.start - 1 };
}

export function pageAfter(shown: LineRange, lineCount: number): LineRange | null {
  if (shown.end >= lineCount) return null;
  return { start: shown.end + 1, end: Math.min(lineCount, shown.end + WINDOW_SIZE) };
}

/** The server's marker on a line it cut at the per-line cap (2,000 chars). */
export const LINE_CUT_MARKER = " … [line truncated]";

export function countCutLines(text: string | null | undefined): number {
  if (!text) return 0;
  let n = 0;
  for (const ln of text.split("\n")) if (ln.endsWith(LINE_CUT_MARKER)) n++;
  return n;
}
