import { Markdown as DSMarkdown } from "@protolabsai/ui/markdown";

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
 * Code-block line numbers default OFF in the DS `<Markdown>` as of `@protolabsai/ui@0.52.1`
 * (protoContent#376) — the DS themes the gutter for Tailwind-purging consumers and no longer
 * needs the console to force `lineNumbers={false}`. Pass an explicit `lineNumbers` prop to opt
 * a numbered code well back in.
 */
export function Markdown({ children }: { children: string }) {
  return <DSMarkdown className="markdown">{children}</DSMarkdown>;
}
