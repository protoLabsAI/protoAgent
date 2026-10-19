import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";
import { Markdown } from "./LazyMarkdown";
import { markForReveal } from "./revealText";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Exact stored three-line reply from #4268, including the inline exponent operator.
const REPRO = [
  "- `calc.py` — added `power(a, b)` returning `a ** b`, matching the existing one-liner style.",
  "- `test_calc.py` — imported `power` and added `test_power` asserting `power(2, 3) == 8`.",
  "- `python -m pytest -q` → **3 passed**.",
].join("\n");

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
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/app/");
});

function renderMessage(content: string, status: ChatMessage["status"], ordered: boolean) {
  const message: ChatMessage = {
    id: "settled-regression", role: "assistant", content, status,
    ...(ordered ? { parts: [{ kind: "text" as const, text: content }] } : {}),
  };
  act(() => root.render(<ChatMessageView message={message} />));
}

function expectRepro() {
  const items = [...container.querySelectorAll(".markdown li")];
  expect(items.map((item) => item.textContent)).toEqual([
    "calc.py — added power(a, b) returning a ** b, matching the existing one-liner style.",
    "test_calc.py — imported power and added test_power asserting power(2, 3) == 8.",
    "python -m pytest -q → 3 passed.",
  ]);
  expect(items[2].querySelector('[data-streamdown="strong"]')?.textContent).toBe("3 passed");
  expect(container.querySelector(".markdown")?.textContent).not.toContain(".**");
}

describe("settled assistant Markdown", () => {
  it("renders the exact issue reply from history and after a fresh mount", () => {
    renderMessage(REPRO, "done", false);
    expectRepro();
    act(() => root.unmount());
    root = createRoot(container);
    renderMessage(REPRO, "done", false);
    expectRepro();
  });

  it.each([false, true])("keeps partial bold functional then settles (ordered parts: %s)", (ordered) => {
    renderMessage("**3 passed", "streaming", ordered);
    expect(container.querySelector('[data-streamdown="strong"]')?.textContent).toBe("3 passed");
    renderMessage(REPRO, "streaming", ordered);
    // Same source, only status changes: repair must not survive the final update.
    renderMessage(REPRO, "done", ordered);
    expectRepro();
  });

  it("disables repair when a client-side reveal finishes", () => {
    let now = 0;
    let frame: FrameRequestCallback = () => {};
    vi.spyOn(performance, "now").mockImplementation(() => now);
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frame = callback;
      return 1;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
    markForReveal("settled-regression");
    renderMessage(REPRO, "done", false);
    now = 300;
    act(() => frame(now));
    expect(container.querySelector(".markdown")?.textContent?.length).toBeGreaterThan(0);
    now = 3000;
    act(() => frame(now));
    expectRepro();
  });

  it("stops repairing earlier text parts while the last part still streams", () => {
    act(() => root.render(<ChatMessageView message={{
      id: "parts", role: "assistant", status: "streaming", content: "",
      parts: [{ kind: "text", text: "**literal" }, { kind: "text", text: "**partial" }],
    }} />));
    const blocks = container.querySelectorAll(".markdown");
    expect(blocks[0].textContent).toBe("**literal");
    expect(blocks[0].querySelector('[data-streamdown="strong"]')).toBeNull();
    expect(blocks[1].querySelector('[data-streamdown="strong"]')?.textContent).toBe("partial");
  });

  it.each(["`a * b`", "`a ** b`", "```text\na * b\na ** b\n```"])(
    "preserves code markers in %s alongside final bold", async (code) => {
      await act(async () => root.render(<Markdown>{`${code}\n\n**3 passed**.`}</Markdown>));
      const renderedCode = container.querySelector("code")?.textContent;
      if (code.includes("```")) {
        // Shiki's line spans and the async plain-code fallback differ in newline text nodes.
        expect(renderedCode).toContain("a * b");
        expect(renderedCode).toContain("a ** b");
      } else {
        expect(renderedCode).toBe(code.slice(1, -1));
      }
      expect(container.querySelector('[data-streamdown="strong"]')?.textContent).toBe("3 passed");
      expect(container.querySelector(".markdown")?.textContent).toMatch(/3 passed\.$/);
    },
  );

  it("preserves unmatched final markers without repairing the source", () => {
    act(() => root.render(<Markdown>{"**unfinished"}</Markdown>));
    expect(container.textContent).toBe("**unfinished");
    expect(container.querySelector('[data-streamdown="strong"]')).toBeNull();
  });

  it("keeps media URL rewriting and math in settled output", async () => {
    window.history.replaceState({}, "", "/app/?__apiPort=54321");
    await act(async () => root.render(<Markdown>{"[download](/media/result.txt)\n\n$x^2$"}</Markdown>));
    expect(container.querySelector("a")?.getAttribute("href")).toBe("http://127.0.0.1:54321/media/result.txt");
    expect(container.querySelector(".katex")).not.toBeNull();
  });
});
