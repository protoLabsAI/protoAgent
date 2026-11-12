// @vitest-environment node
// copyText reports whether the clipboard write landed, and never rejects (#4733 QA).
import { afterEach, describe, expect, it, vi } from "vitest";

import { copyText } from "./clipboard";

function stubClipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });
}

afterEach(() => {
  stubClipboard(undefined);
});

describe("copyText", () => {
  it("resolves true once writeText resolves", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard({ writeText });
    await expect(copyText("abc")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("abc");
  });

  it("resolves false when writeText rejects", async () => {
    stubClipboard({ writeText: vi.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError")) });
    await expect(copyText("abc")).resolves.toBe(false);
  });

  it("resolves false when the Clipboard API is missing (insecure context)", async () => {
    stubClipboard(undefined);
    await expect(copyText("abc")).resolves.toBe(false);
  });

  it("resolves false when writeText throws synchronously", async () => {
    stubClipboard({
      writeText: () => {
        throw new Error("boom");
      },
    });
    await expect(copyText("abc")).resolves.toBe(false);
  });
});
