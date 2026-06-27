import type { ReactNode } from "react";

// A document the full-screen viewer can render (ADR 0062). Deliberately generic so any
// surface — a background-agent report, an Activity entry, a long tool output, a knowledge
// doc — opens the SAME reader via `openDocument(spec)`. Body resolution, in priority order:
//   1. `render()` — an arbitrary React body (escape hatch for non-markdown future views)
//   2. `load()`   — async markdown, fetched when the viewer opens (e.g. a full report by id)
//   3. `content`  — inline markdown
export type DocumentSpec = {
  /** Heading shown in the viewer's title bar. */
  title: string;
  /** Optional sub-line under the title (source, timestamp, origin…). */
  subtitle?: ReactNode;
  /** Inline markdown body. */
  content?: string;
  /** Async markdown body — resolved on open; takes precedence over `content`. */
  load?: () => Promise<string>;
  /** Render an arbitrary body instead of markdown — wins over `load`/`content`. */
  render?: () => ReactNode;
};
