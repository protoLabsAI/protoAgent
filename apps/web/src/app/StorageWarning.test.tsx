import { act, createElement as h } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { QuotaStorage } from "../lib/quotaStorage.testkit";
import { __resetStorageSeamForTests, writeKey } from "../lib/storage";
import { StorageWarning } from "./StorageWarning";

it("keeps unsaved-cache warning visible until a write actually frees space", () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const local = new QuotaStorage(1000);
  vi.stubGlobal("localStorage", local);
  __resetStorageSeamForTests();
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    act(() => root.render(h(StorageWarning)));
    expect(container.textContent).toBe("");
    local.setItem("protoagent.ui", "x".repeat(100));
    local.setItem("plugin.hog", "x".repeat(370));
    act(() => { writeKey("local", "protoagent.chat.sessions", "x".repeat(50)); });
    expect(container.textContent).toContain("Browser storage is full");
    expect(container.textContent).toContain("may not survive a reload");
    act(() => { writeKey("local", "protoagent.ui", "y".repeat(100)); });
    expect(container.textContent).toContain("Browser storage is full");
    act(() => { writeKey("local", "protoagent.ui", "{}"); });
    expect(container.textContent).toBe("");
  } finally {
    act(() => root.unmount());
    vi.unstubAllGlobals();
    __resetStorageSeamForTests();
  }
});
