import { describe, it, expect } from "vitest";
import { migrateUiState } from "./uiStore";

// v1→v2 migration: the obsolete `railOf` (per-surface side map) must be dropped
// so `railOrder` falls back to the default via the store's merge. A stale
// `railOf` surviving migration would resurrect the old rail layout.

describe("migrateUiState", () => {
  it("drops railOf and keeps the rest", () => {
    const out = migrateUiState({ railOf: { chat: "left" }, leftActive: "chat", rightWidth: 320 });
    expect(out).toEqual({ leftActive: "chat", rightWidth: 320 });
    expect(out).not.toHaveProperty("railOf");
  });

  it("passes through an object that has no railOf", () => {
    expect(migrateUiState({ leftActive: "chat" })).toEqual({ leftActive: "chat" });
  });

  // v2→v3 (ADR 0048): the flat `settingsTab` is replaced by `settingsScope` +
  // `settingsSection`; a stale `settingsTab` must be dropped so the new defaults apply.
  it("drops the obsolete settingsTab", () => {
    const out = migrateUiState({ settingsTab: "host", rightWidth: 320 }) as Record<string, unknown>;
    expect(out).not.toHaveProperty("settingsTab");
    expect(out).toEqual({ rightWidth: 320 });
  });

  it("does not mutate the input object", () => {
    const input = { railOf: { chat: "left" }, leftActive: "chat" };
    migrateUiState(input);
    expect(input).toHaveProperty("railOf");
  });

  it("returns null and non-objects unchanged", () => {
    expect(migrateUiState(null)).toBeNull();
    expect(migrateUiState("nope")).toBe("nope");
    expect(migrateUiState(undefined)).toBeUndefined();
  });
});
