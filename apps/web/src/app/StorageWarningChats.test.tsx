import { act, createElement as h } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

// A failing chat save to IndexedDB (epic #4981) shows the same warning as a full localStorage.
const failing = vi.hoisted(() => ({ value: true, idb: true }));
vi.mock("../chat/chat-store", () => ({
  transcriptSaveFailing: () => failing.value,
  subscribeTranscriptSaveFailing: () => () => {},
  transcriptStoreIfOpen: () => (failing.idb ? ({} as object) : null),
}));

it("warns when chat saves to IndexedDB are failing, with the Settings → Storage pointer", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const { StorageWarning } = await import("./StorageWarning");
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    act(() => root.render(h(StorageWarning)));
    expect(container.textContent).toContain("New messages aren't being saved in this browser");
    expect(container.textContent).toContain("Free up space in Settings → Storage");
  } finally {
    act(() => root.unmount());
  }
});

it("a full localStorage while chats save to IndexedDB says only settings are affected", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  failing.value = false;
  failing.idb = true;
  const storage = await import("../lib/storage");
  vi.spyOn(storage, "storagePressure").mockReturnValue({ state: "failing" });
  const { StorageWarning } = await import("./StorageWarning");
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    act(() => root.render(h(StorageWarning)));
    expect(container.textContent).toContain("Some console settings aren't being saved in this browser");
    expect(container.textContent).toContain("Your chats are saving.");
    expect(container.textContent).not.toContain("New messages aren't being saved");
  } finally {
    act(() => root.unmount());
    vi.restoreAllMocks();
  }
});
