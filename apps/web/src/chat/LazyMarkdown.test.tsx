// The lazily loaded markdown renderer must never paint the raw markdown SOURCE. Its Suspense
// fallback used to render `{children}` as plain text, so the first Markdown mount of a fresh page
// showed `Done — - **bold** … \`code\`` on one line for ~0.3s before the rendered list replaced it
// (the launch-demo flash on a tool turn). The real `./Markdown` is swapped for a gated stub so
// each test controls exactly when the module arrives.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const gate = vi.hoisted(() => {
  let open!: () => void;
  const ready = new Promise<void>((r) => (open = r));
  return { ready, open: () => open() };
});

vi.mock("./Markdown", async () => {
  await gate.ready;
  return {
    Markdown: ({ children }: { children: string }) => h("div", { "data-testid": "rendered" }, children.replace(/\*\*/g, "")),
  };
});

const SOURCE = "Done — appended:\n\n- **protoAgent** — pinned in `plugins.lock`";

// vitest.setup.ts preloads the REAL LazyMarkdown for every suite; drop that instance so the
// imports below get a fresh module wired to the gated stub.
beforeAll(() => {
  vi.resetModules();
});

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("LazyMarkdown", () => {
  it("renders nothing (never the raw source) while the renderer is still loading", async () => {
    const { Markdown } = await import("./LazyMarkdown");
    act(() => root.render(h(Markdown, null, SOURCE)));
    expect(container.querySelector("[data-testid=rendered]")).toBeNull();
    expect(container.textContent).toBe("");
    expect(container.textContent).not.toContain("**");
  });

  it("once loaded, a fresh mount renders on its FIRST commit — no fallback frame", async () => {
    gate.open();
    await (await import("./LazyMarkdown")).preloadMarkdown();
    vi.resetModules(); // a brand-new lazy() whose first mount happens AFTER the module arrived
    const fresh = await import("./LazyMarkdown");
    await fresh.preloadMarkdown();
    // Synchronous act: a pending (plain-Promise) lazy would leave the fallback committed here.
    act(() => root.render(h(fresh.Markdown, null, SOURCE)));
    expect(container.querySelector("[data-markdown-pending]")).toBeNull();
    expect(container.querySelector("[data-testid=rendered]")?.textContent).toContain("protoAgent");
    expect(container.textContent).not.toContain("**");
  });
});
