import { Markdown as DSMarkdown } from "@protolabsai/ui/markdown";

import { rehypeAbsolutizeServerUrls } from "./mediaUrls";

// Module-level for a stable array identity across renders. The DS appends these AFTER its
// own defaults (GFM/sanitize/harden/KaTeX), so the URL rewrite sees the final tree.
const REHYPE_PLUGINS = [rehypeAbsolutizeServerUrls];

/**
 * Assistant message markdown — the DS `<Markdown>` (`@protolabsai/ui/markdown`, ≥0.48),
 * which owns the brand styling for streamdown's prose AND its interactive chrome (code /
 * table action buttons, themed + re-pinned), wires KaTeX math + GFM, and renders ```mermaid
 * as a themed code block (live diagrams are an opt-in `renderMermaid`). Chrome defaults to
 * copy-only — download/fullscreen are off for a chat bubble. Replaces the console's
 * hand-rolled streamdown usage (protoContent#298).
 *
 * `className="markdown"` rides the same element the DS scopes as `.pl-markdown`, so existing
 * `.markdown` selectors (e2e + message-layout) keep matching.
 *
 * `rehypeAbsolutizeServerUrls` re-targets server-relative `/media/` + `/plugins/` URLs at
 * the focused agent (#1946) — a no-op in a same-origin browser console, load-bearing in the
 * desktop shell (webview origin ≠ agent server) and in fleet remote-agent views.
 *
 * Code-block line numbers default OFF in the DS `<Markdown>` as of `@protolabsai/ui@0.52.1`
 * (protoContent#376) — the DS themes the gutter for Tailwind-purging consumers and no longer
 * needs the console to force `lineNumbers={false}`. Pass an explicit `lineNumbers` prop to opt
 * a numbered code well back in.
 *
 * Currency-as-math is handled by the DS itself as of `@protolabsai/ui@0.55.1`
 * (protoContent#456): a `$` before a digit is escaped by default, so "$180M … $600M" no
 * longer parses the span between two amounts as KaTeX math while real math (`$x^2$`, `$$…$$`)
 * survives. This replaces the console's old `escapeCurrencyDollars` pre-processing (#1983) —
 * the DS ported that exact guard on-by-default. Opt out per the DS `math` prop if ever needed.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <DSMarkdown className="markdown" rehypePlugins={REHYPE_PLUGINS}>
      {children}
    </DSMarkdown>
  );
}
