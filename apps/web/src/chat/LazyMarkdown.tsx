import { lazy, Suspense, type ComponentType } from "react";

// The markdown pipeline (the DS `<Markdown>` over streamdown — Shiki + KaTeX + mermaid) is
// the heaviest dependency in the app. Load it lazily so it isn't in the initial chunk.
//
// The fallback must NEVER paint the raw markdown source. It used to render `{children}` as
// plain text, and React lazy suspends on its FIRST mount even when the module is already in
// hand (the factory's promise settles a microtask later) — after which React 19 holds the
// committed fallback for its ~300ms reveal throttle. So the first Markdown mount of a fresh
// page painted `Done — - **bold** … \`code\`` as one raw line for ~0.3s: invisible on a
// streamed answer (its first delta is usually plain words), glaring on a tool turn whose whole
// answer lands at once. Now:
//   1. the module is fetched as soon as this file loads (`preloadMarkdown` below), so it is
//      resolved long before the first assistant text arrives;
//   2. once resolved, the lazy factory hands React a SYNCHRONOUS thenable, so the first mount
//      never suspends at all;
//   3. if text does arrive before the module (a slow split chunk), the fallback is an EMPTY
//      markdown scope — the text appears rendered a beat later, never as raw source.
type MarkdownComponent = ComponentType<{ children: string }>;

let loaded: { default: MarkdownComponent } | null = null;
let loading: Promise<{ default: MarkdownComponent }> | null = null;

/** Start (or join) the markdown chunk fetch. Idempotent; safe to call any time. */
export function preloadMarkdown(): Promise<{ default: MarkdownComponent }> {
  if (!loading) {
    loading = import("./Markdown").then((m) => (loaded = { default: m.Markdown }));
    // A failed fetch must not poison every later mount: let the next call retry.
    loading.catch(() => {
      loading = null;
    });
  }
  return loading;
}

const MarkdownImpl = lazy(() =>
  loaded
    ? // React's lazy reads `_status` right after calling `.then` — a thenable that resolves
      // synchronously makes the very first render use the module with no suspension.
      ({ then: (resolve: (m: { default: MarkdownComponent }) => void) => resolve(loaded!) } as unknown as Promise<{
        default: MarkdownComponent;
      }>)
    : preloadMarkdown(),
);

// Warm it now: by the time any message renders, the lazy factory takes the synchronous path.
void preloadMarkdown().catch(() => {});

export function Markdown({ children }: { children: string }) {
  return (
    <Suspense fallback={<div className="pl-markdown markdown" aria-busy="true" data-markdown-pending="" />}>
      <MarkdownImpl>{children}</MarkdownImpl>
    </Suspense>
  );
}
