import { lazy, Suspense } from "react";

// The code pane's surface entry (ADR 0112). The pane itself — and with it the highlighter,
// @pierre/diffs over Shiki — is a lazy chunk, fetched the first time the surface is shown,
// so a console that never opens a file never pays for it. (No vite manualChunks: the lazy
// import IS the split — settingsPalette.test.ts guards the config.)
const CodePane = lazy(() => import("./CodePane"));

export function CodeSurface() {
  return (
    <Suspense
      fallback={
        <section className="panel stage-panel code-pane" data-testid="code-pane-loading">
          <div className="code-pane__status">Loading the code viewer…</div>
        </section>
      }
    >
      <CodePane />
    </Suspense>
  );
}
