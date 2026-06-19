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
      railOrder: { left: ["chat", "schedule", "plugins", "box", "settings"], right: ["beads"] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.left).toEqual(["chat", "schedule", "plugins", "settings"]);
    expect(out.railOrder.left).not.toContain("box");
    expect(out.railOrder.right).toEqual(["beads"]);
  });

  // v7: "schedule" is a top-level rail surface again (un-fold from #1075). A persisted
  // layout that lost it (it was pruned/folded) gets it re-injected where "activity" was —
  // then v8 prunes "activity" itself, leaving "schedule" in its place.
  it("restores the 'schedule' rail surface to a layout that lacks it", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "activity", "settings"], right: ["beads", "goals"] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.left).toEqual(["chat", "schedule", "settings"]);
  });

  // …but if the user keeps "schedule" on some dock already, don't duplicate it.
  it("keeps a user-placed 'schedule' where it is", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "activity", "settings"], right: ["beads", "goals", "schedule"] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.right).toContain("schedule");
    expect(out.railOrder.left).not.toContain("schedule");
  });

  // v8 (2026-06 IA pass): Activity moved off the rail to a utility-bar widget. Prune
  // "activity" from every dock + the mobile quick-bar so it doesn't linger as a dead id.
  it("prunes 'activity' from the rails and quick-bar", () => {
    const out = migrateUiState({
      // "settings" present so the v9 re-add below is a no-op — this test is about activity.
      railOrder: { left: ["chat", "activity", "schedule"], right: ["activity", "beads", "settings"], bottom: ["activity"] },
      quickBar: ["chat", "activity", "knowledge"],
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] }; quickBar: string[] };
    expect(out.railOrder.left).not.toContain("activity");
    expect(out.railOrder.right).not.toContain("activity");
    expect(out.railOrder.bottom).not.toContain("activity");
    expect(out.railOrder.left).toEqual(["chat", "schedule"]);
    expect(out.quickBar).toEqual(["chat", "knowledge"]);
  });

  // v9 (2026-06-18 IA pass): Workspace settings became a rail surface; re-add "settings"
  // to a persisted layout that lacks it so existing users don't lose the Settings icon.
  it("restores the 'settings' rail surface to a layout that lacks it", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "schedule"], right: ["beads", "goals", "plugins"], bottom: [] },
    }) as { railOrder: { left: string[] } };
    // No "plugins" on the left rail → appended to the end of it.
    expect(out.railOrder.left).toEqual(["chat", "schedule", "settings"]);
  });

  it("re-adds 'settings' right after 'plugins' on the left when present", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "schedule", "plugins", "knowledge"], right: ["beads"], bottom: [] },
    }) as { railOrder: { left: string[] } };
    expect(out.railOrder.left).toEqual(["chat", "schedule", "plugins", "settings", "knowledge"]);
  });

  it("keeps a user-placed 'settings' where it is", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "schedule"], right: ["settings", "beads"], bottom: [] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.right).toContain("settings");
    expect(out.railOrder.left).not.toContain("settings");
  });

  // v5→v6 (bottom dock): railOrder gains a `bottom` dock; add the empty array to a
  // persisted layout that predates it.
  it("adds the bottom dock to a pre-v6 railOrder", () => {
    // (schedule already present so the v7 inject is a no-op — keep this test on the dock)
    const out = migrateUiState({
      railOrder: { left: ["chat", "schedule", "settings"], right: ["beads"] },
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] } };
    expect(out.railOrder.bottom).toEqual([]);
    expect(out.railOrder.left).toEqual(["chat", "schedule", "settings"]);
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
