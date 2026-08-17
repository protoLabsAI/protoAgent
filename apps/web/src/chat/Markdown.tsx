import { Markdown as DSMarkdown } from "@protolabsai/ui/markdown";

import { rehypeAbsolutizeServerUrls } from "./mediaUrls";
import { useSmoothReveal } from "./useSmoothReveal";

// Module-level for a stable array identity across renders. The DS appends these AFTER its
// own defaults (GFM/sanitize/harden/KaTeX), so the URL rewrite sees the final tree.
const REHYPE_PLUGINS = [rehypeAbsolutizeServerUrls];

// Streaming token fade (#2769), two cooperating halves:
// 1. `useSmoothReveal` paces the SOURCE — a steadily-advancing prefix of the accumulated
//    markdown (append-only by construction), which is what actually smooths bursty chunk
//    arrival AND keeps the fade correct: streamdown's animate plugin marks already-visible
//    content by a flat char offset in tree-visit order, which GFM restructuring (footnote
//    hoisting especially) breaks — first QA round saw later lines fading while earlier ones
//    were still arriving, and footnote links popping in unfaded. Pacing the source pins the
//    changing region to the parse tail, so the offset heuristic holds.
// 2. The fade itself stays short and stagger-free — with the reveal supplying the motion,
//    the fade is just softening on the freshly-revealed words, not the smoothing mechanism.
// Settled messages render span-free (`isAnimating` false drops the plugin from the pipeline).
// Module-level for stable identity — an inline object would re-key the plugin per render.
const ANIMATED = { animation: "fadeIn", sep: "word", duration: 180, easing: "ease-out", stagger: 0 } as const;

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
export function Markdown({ children, streaming = false }: { children: string; streaming?: boolean }) {
  const shown = useSmoothReveal(children, streaming);
  return (
    <DSMarkdown className="markdown" rehypePlugins={REHYPE_PLUGINS} animated={ANIMATED} isAnimating={streaming}>
      {shown}
    </DSMarkdown>
  );
}
