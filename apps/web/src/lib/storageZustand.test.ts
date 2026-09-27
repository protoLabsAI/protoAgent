import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QuotaStorage } from "./quotaStorage.testkit";

// ADR 0114 D1 — the three persisted zustand stores under a FULL quota. zustand 5's `persist`
// calls `storage.setItem` synchronously inside every `set()` (and on migrate-on-hydrate) with
// no catch; before the seam, `useUI` setters in App's mount effects threw straight into the
// root error boundary, and Reload looped back to the crash.

let local: QuotaStorage;

beforeEach(() => {
  vi.resetModules(); // each case re-hydrates the stores from the storage it installs
  local = new QuotaStorage(4000);
  vi.stubGlobal("localStorage", local);
  vi.stubGlobal("sessionStorage", new QuotaStorage(4000));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Fill the quota to within `room` bytes with an unregistered (never-evicted) key. */
function fill(room: number) {
  const used = local.used();
  const chars = Math.floor((local.quota - used - room) / 2) - "plugin.hog".length;
  local.setItem("plugin.hog", "x".repeat(chars));
}

describe("persisted zustand stores never throw on a full quota", () => {
  it("useUI: migrate-on-hydrate (version mismatch) writes inside hydration — and doesn't throw", async () => {
    // A stale-version layout blob: hydration migrates it and zustand immediately setItem()s the
    // result — the write lands on a full quota, inside module init.
    local.setItem("protoagent.ui", JSON.stringify({ state: { surface: "activity", rightWidth: 320 }, version: 1 }));
    fill(10);
    const { useUI } = await import("../state/uiStore");
    const { storagePressure } = await import("./storage");
    expect(useUI.getState().surface).toBe("activity"); // hydrated + migrated in memory
    expect(storagePressure().state).toBe("failing"); // the write was attempted, failed, latched
  });

  it("useUI: setters (the mount-effect path) don't throw", async () => {
    fill(10);
    const { useUI } = await import("../state/uiStore");
    expect(() => {
      useUI.getState().setSurface("activity");
      useUI.getState().setRightWidth(333);
      useUI.getState().setPluginDot("plugin:x:y", true);
    }).not.toThrow();
    expect(useUI.getState().surface).toBe("activity"); // in-memory state still moves
  });

  it("useUI: a no-op set doesn't rewrite the layout blob", async () => {
    const { useUI } = await import("../state/uiStore");
    useUI.getState().setSurface("activity");
    const writes = local.setCalls;
    useUI.getState().setSurface("activity");
    useUI.getState().setPluginDot("plugin:x:y", false); // already off → `return s`
    useUI.getState().setPluginBackground("plugin:x:y", true); // not persisted (partialized out)
    expect(local.setCalls).toBe(writes);
    useUI.getState().setSurface("chat");
    expect(local.setCalls).toBe(writes + 1);
  });

  it("useKeybindingOverrides goes through the seam (it had no storage: option)", async () => {
    fill(10);
    const { useKeybindingOverrides } = await import("../keybindings/overrides");
    expect(() => useKeybindingOverrides.getState().setBinding("palette.open", "mod+j")).not.toThrow();
    expect(useKeybindingOverrides.getState().overrides["palette.open"]).toBe("mod+j");
  });

  it("createUISlice stores don't throw", async () => {
    fill(10);
    const { createUISlice } = await import("../ext/uiStateRegistry");
    const useSlice = createUISlice("quota-test", { open: false });
    expect(() => useSlice.setState({ open: true })).not.toThrow();
    expect(useSlice.getState().open).toBe(true);
  });

  it("with room, they still persist (the seam is transparent)", async () => {
    const { useKeybindingOverrides } = await import("../keybindings/overrides");
    useKeybindingOverrides.getState().setBinding("palette.open", "mod+j");
    expect(JSON.parse(local.getItem("protoagent.keybindings") ?? "{}").state.overrides).toEqual({
      "palette.open": "mod+j",
    });
  });
});
