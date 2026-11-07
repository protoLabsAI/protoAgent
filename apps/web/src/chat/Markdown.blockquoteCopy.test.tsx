import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Markdown } from "./LazyMarkdown";

// #4773 — @protolabsai/ui@0.70.0's `<Markdown>` gives every OUTERMOST blockquote a copy button
// (streamdown's CodeBlockCopyButton, accessible name "Copy quote"), the same chrome the console
// already gets on code fences. The feature ships purely through the dependency bump — there is no
// console-side code in Markdown.tsx — so this guard renders assistant markdown through the real
// LazyMarkdown pipeline (same harness as Markdown.settled.test.tsx) and proves the behavior the
// bump is adopted for: one button per top-level quote, plain-text copy (no `>` / inline markers,
// paragraphs blank-line-joined), outermost-only on a nested quote, and the code-fence copy button
// still alongside it.

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// streamdown lazy-reveals blocks via IntersectionObserver; jsdom ships none, so stub a no-op.
if (!("IntersectionObserver" in globalThis)) {
  (globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  };
}

// A two-paragraph quote with inline formatting (bold + a `>` on the blank separator line) and a
// fenced code block, so the same render carries both copy affordances.
const QUOTE = "> first paragraph\n>\n> second **bold** paragraph";
const CODE = "```js\nconst a = 1;\n```";

let container: HTMLDivElement;
let root: Root;
let writes: string[];

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  writes = [];
  // The copy button calls navigator.clipboard.writeText; jsdom has no real clipboard.
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: vi.fn(async (text: string) => void writes.push(text)) },
    configurable: true,
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

async function render(markdown: string) {
  await act(async () => root.render(<Markdown>{markdown}</Markdown>));
  await act(async () => {}); // flush the lazy module body + effects
}

const quoteButtons = () => [
  ...container.querySelectorAll<HTMLButtonElement>('button[aria-label="Copy quote"]'),
];

// The code fence's own copy button shares streamdown's hook but is NOT the quote button.
const codeCopyButton = () =>
  container.querySelector('[data-streamdown="code-block-copy-button"]:not([aria-label="Copy quote"])');

async function click(el: Element) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  });
  await act(async () => {}); // flush the async copy handler
}

describe("assistant Markdown blockquote copy button (#4773)", () => {
  it("renders exactly one 'Copy quote' button for the quote and keeps the code-block copy button", async () => {
    await render(`${QUOTE}\n\n${CODE}`);
    const buttons = quoteButtons();
    expect(buttons).toHaveLength(1);
    // It is streamdown's CodeBlockCopyButton, reused via the DS blockquote renderer.
    expect(buttons[0].getAttribute("data-streamdown")).toBe("code-block-copy-button");
    expect(container.querySelector('[data-streamdown="blockquote"]')).not.toBeNull();
    // The fenced code block still renders its own (separate) copy button.
    expect(codeCopyButton()).not.toBeNull();
  });

  it("copies the quote as plain text — no `>`, no inline markers, paragraphs blank-line-joined", async () => {
    await render(QUOTE);
    const buttons = quoteButtons();
    expect(buttons).toHaveLength(1);
    await click(buttons[0]);
    expect(writes).toEqual(["first paragraph\n\nsecond bold paragraph"]);
    expect(writes[0]).not.toContain(">");
    expect(writes[0]).not.toContain("**");
  });

  it("puts the button on the outermost quote only — a nested quote yields one, not two", async () => {
    await render("> outer\n>> inner");
    expect(quoteButtons()).toHaveLength(1);
  });
});
