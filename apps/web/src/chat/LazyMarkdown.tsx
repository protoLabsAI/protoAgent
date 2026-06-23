import { lazy, Suspense } from "react";

// The markdown pipeline (streamdown — which bundles Shiki + mermaid + katex) is the
// heaviest dependency in the app. Load it lazily so it isn't in the initial chunk — it
// only matters once an assistant message renders. Until the chunk arrives, fall back to
// the raw text (a blink at most; then the same `.markdown` container takes over).
const MarkdownImpl = lazy(() => import("./Markdown").then((m) => ({ default: m.Markdown })));

export function Markdown({ children }: { children: string }) {
  return (
    <Suspense fallback={<div className="markdown">{children}</div>}>
      <MarkdownImpl>{children}</MarkdownImpl>
    </Suspense>
  );
}
