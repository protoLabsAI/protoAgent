import { lazy, Suspense } from "react";

// The markdown pipeline (the DS `<Markdown>` over streamdown — Shiki + KaTeX + mermaid) is
// the heaviest dependency in the app. Load it lazily so it isn't in the initial chunk — it
// only matters once an assistant message renders. Until the chunk arrives, fall back to the
// raw text in the DS `.pl-markdown` scope (a blink at most; the real renderer then takes over).
const MarkdownImpl = lazy(() => import("./Markdown").then((m) => ({ default: m.Markdown })));

export function Markdown({ children }: { children: string }) {
  return (
    <Suspense fallback={<div className="pl-markdown markdown">{children}</div>}>
      <MarkdownImpl>{children}</MarkdownImpl>
    </Suspense>
  );
}
