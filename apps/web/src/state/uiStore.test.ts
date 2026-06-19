import { beforeEach, describe, it, expect } from "vitest";
import { migrateUiState, useUI } from "./uiStore";

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

  // "box" folded into Settings ▸ Global — prune the obsolete rail surface from a
  // persisted railOrder rather than leaving a dead rail id with no surface metadata.
  it("prunes the obsolete 'box' rail surface", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "knowledge", "box", "settings"], right: ["work"] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.left).toEqual(["chat", "knowledge", "settings"]);
    expect(out.railOrder.left).not.toContain("box");
    expect(out.railOrder.right).toEqual(["work"]);
  });

  // (The v7 "restore schedule" behavior is gone: schedule folded into the Work hub in v11,
  // which prunes it — so re-adding it would be undone. Covered by the v11 fold test below.)

  // v8 (2026-06 IA pass): Activity moved off the rail to a utility-bar widget. Prune
  // "activity" from every dock + the mobile quick-bar so it doesn't linger as a dead id.
  it("prunes 'activity' from the rails and quick-bar", () => {
    const out = migrateUiState({
      // "settings"/"work" present so the v9/v11 add steps are no-ops — this test is about activity.
      railOrder: { left: ["chat", "activity", "knowledge"], right: ["activity", "work", "settings"], bottom: ["activity"] },
      quickBar: ["chat", "activity", "knowledge"],
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] }; quickBar: string[] };
    expect(out.railOrder.left).not.toContain("activity");
    expect(out.railOrder.right).not.toContain("activity");
    expect(out.railOrder.bottom).not.toContain("activity");
    expect(out.railOrder.left).toEqual(["chat", "knowledge"]);
    expect(out.quickBar).toEqual(["chat", "knowledge"]);
  });

  // v9 (2026-06-18 IA pass): Workspace settings became a rail surface; re-add "settings"
  // to a persisted layout that lacks it so existing users don't lose the Settings icon.
  it("restores the 'settings' rail surface to a layout that lacks it", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "knowledge"], right: ["work"], bottom: [] },
    }) as { railOrder: { left: string[] } };
    // No "plugins" anchor on the left rail → settings appended to the end of it.
    expect(out.railOrder.left).toEqual(["chat", "knowledge", "settings"]);
  });

  it("re-adds 'settings' where 'plugins' was, then v10 prunes 'plugins'", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "schedule", "plugins", "knowledge"], right: ["beads"], bottom: [] },
    }) as { railOrder: { left: string[] } };
    // v9 inserts settings after the (legacy) plugins anchor; v10 removes plugins, v11 schedule.
    expect(out.railOrder.left).toEqual(["chat", "settings", "knowledge"]);
  });

  // v10 (2026-06): the Plugins manager moved into Settings ▸ Plugins. Prune "plugins" from
  // every dock + the quick-bar so it doesn't linger as a dead rail id.
  it("prunes 'plugins' from the rails and quick-bar", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "schedule", "plugins", "settings"], right: ["plugins", "beads"], bottom: ["plugins"] },
      quickBar: ["chat", "plugins", "knowledge"],
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] }; quickBar: string[] };
    expect(out.railOrder.left).not.toContain("plugins");
    expect(out.railOrder.right).not.toContain("plugins");
    expect(out.railOrder.bottom).not.toContain("plugins");
    expect(out.railOrder.left).toEqual(["chat", "settings"]);
    expect(out.quickBar).toEqual(["chat", "knowledge"]);
  });

  it("keeps a user-placed 'settings' where it is", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "knowledge"], right: ["settings", "work"], bottom: [] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.right).toContain("settings");
    expect(out.railOrder.left).not.toContain("settings");
  });

  // v11 (2026-06): Beads + Goals + Schedule folded into the unified "work" hub.
  it("folds beads/goals/schedule into the 'work' hub", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "schedule", "knowledge", "settings"], right: ["beads", "goals"], bottom: [] },
      rightPanel: "beads",
      quickBar: ["chat", "beads", "knowledge"],
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] }; rightPanel: string; quickBar: string[] };
    for (const id of ["beads", "goals", "schedule"]) {
      expect(out.railOrder.left).not.toContain(id);
      expect(out.railOrder.right).not.toContain(id);
      expect(out.quickBar).not.toContain(id);
    }
    expect(out.railOrder.left).toEqual(["chat", "knowledge", "settings"]);
    expect(out.railOrder.right).toEqual(["work"]);
    expect(out.rightPanel).toBe("work");
    expect(out.quickBar).toEqual(["chat", "knowledge"]);
  });

  it("keeps a user-placed 'work' and doesn't duplicate it", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "work"], right: ["settings"], bottom: [] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.left.filter((x) => x === "work")).toHaveLength(1);
    expect(out.railOrder.right).not.toContain("work");
  });

  // v5→v6 (bottom dock): railOrder gains a `bottom` dock; add the empty array to a
  // persisted layout that predates it.
  it("adds the bottom dock to a pre-v6 railOrder", () => {
    // (schedule already present so the v7 inject is a no-op — keep this test on the dock)
    const out = migrateUiState({
      railOrder: { left: ["chat", "knowledge", "settings"], right: ["work"] },
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] } };
    expect(out.railOrder.bottom).toEqual([]);
    expect(out.railOrder.left).toEqual(["chat", "knowledge", "settings"]);
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

// reconcilePluginViews keeps plugin views as railOrder members: append new ones at
// their manifest side, prune uninstalled ones — and NEVER move an id the operator
// already placed. The manifest `placement` is a default for first appearance, not
// an override of a persisted drag-and-drop layout. (The App-side caller must also
// gate on the plugin list having LOADED: reconciling against the boot-time empty
// set would prune every persisted entry and the reload would re-seed by manifest —
// the layout-wipe bug this contract pins down.)
describe("reconcilePluginViews", () => {
  const seed = (left: string[], right: string[], bottom: string[] = []) =>
    useUI.setState({ railOrder: { left, right, bottom } });

  beforeEach(() => seed(["chat", "plugin:doom:panel"], ["beads", "plugin:board:board", "notes"]));

  it("keeps a moved view at its persisted side and position despite its declared side", () => {
    // board's manifest says right→ but suppose the operator dragged doom to the left
    // already; both views re-declare their manifest sides on every reconcile.
    useUI.getState().reconcilePluginViews([
      { id: "plugin:doom:panel", side: "right" }, // manifest says right; operator put it LEFT
      { id: "plugin:board:board", side: "right" },
    ]);
    expect(useUI.getState().railOrder.left).toEqual(["chat", "plugin:doom:panel"]);
    expect(useUI.getState().railOrder.right).toEqual(["beads", "plugin:board:board", "notes"]);
  });

  it("keeps mid-rail positions (no prune/re-append shuffle)", () => {
    useUI.getState().reconcilePluginViews([{ id: "plugin:board:board", side: "right" }, { id: "plugin:doom:panel", side: "left" }]);
    // board stays BETWEEN beads and notes — not re-appended to the bottom.
    expect(useUI.getState().railOrder.right).toEqual(["beads", "plugin:board:board", "notes"]);
  });

  it("appends a NEW view at its declared side", () => {
    useUI.getState().reconcilePluginViews([
      { id: "plugin:doom:panel", side: "left" },
      { id: "plugin:board:board", side: "right" },
      { id: "plugin:browser:panel", side: "right" },
    ]);
    expect(useUI.getState().railOrder.right).toEqual(["beads", "plugin:board:board", "notes", "plugin:browser:panel"]);
  });

  it("prunes a view absent from a non-empty set, leaving core surfaces alone", () => {
    useUI.getState().reconcilePluginViews([{ id: "plugin:doom:panel", side: "left" }]);
    expect(useUI.getState().railOrder.left).toEqual(["chat", "plugin:doom:panel"]);
    expect(useUI.getState().railOrder.right).toEqual(["beads", "notes"]);
  });

  it("prunes everything on an empty set — why the caller must gate on loaded", () => {
    useUI.getState().reconcilePluginViews([]);
    expect(useUI.getState().railOrder.left).toEqual(["chat"]);
    expect(useUI.getState().railOrder.right).toEqual(["beads", "notes"]);
  });
});
