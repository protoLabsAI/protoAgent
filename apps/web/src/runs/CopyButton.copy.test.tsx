// The run drawer's copy buttons say "Copied" only once the clipboard write landed.
// They used to flip to "Copied" right away and swallow the rejection.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "./RunInspector";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

function stubClipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  stubClipboard(undefined);
});

async function click(): Promise<HTMLButtonElement> {
  act(() => root.render(h(CopyButton, { text: "run-123", label: "Copy id", testId: "copy" })));
  const btn = container.querySelector<HTMLButtonElement>('[data-testid="copy"]');
  if (!btn) throw new Error("no copy button");
  await act(async () => {
    btn.click();
  });
  return btn;
}

describe("run drawer CopyButton", () => {
  it("shows Copied once the write resolves", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard({ writeText });
    const btn = await click();
    expect(writeText).toHaveBeenCalledWith("run-123");
    expect(btn.dataset.copyOutcome).toBe("copied");
    expect(btn.textContent).toContain("Copied");
  });

  it("shows Copy failed when the write rejects", async () => {
    stubClipboard({ writeText: vi.fn().mockRejectedValue(new Error("denied")) });
    const btn = await click();
    expect(btn.dataset.copyOutcome).toBe("failed");
    expect(btn.textContent).toContain("Copy failed");
  });

  it("shows Copy failed when the Clipboard API is missing", async () => {
    stubClipboard(undefined);
    const btn = await click();
    expect(btn.dataset.copyOutcome).toBe("failed");
  });
});
