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
 * `lineNumbers={false}`: streamdown defaults line numbers ON, but renders them purely via
 * `before:` Tailwind utilities with NO `data-streamdown` hook — so the DS's attribute-selector
 * theming can't reach them and the console (which purges streamdown's inert Tailwind by design)
 * leaves a broken, unstyled gutter that pushes code sideways. Code reads clean without them.
 * (DS gap — the DS `<Markdown>` should default this off since it can't theme the gutter;
 * file on protoContent.)
 */
export function Markdown({ children }: { children: string }) {
  return (
    <DSMarkdown className="markdown" lineNumbers={false}>
      {children}
    </DSMarkdown>
  );
}
