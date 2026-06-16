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

  // v3→v4 (PR4): the new "box" rail surface is injected into a pre-v4 railOrder
  // (before "settings") so an existing drag-and-drop layout gains it rather than
  // hiding it.
  it("injects the Box surface into a pre-v4 railOrder, before settings", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "plugins", "settings"], right: ["beads"] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.left).toEqual(["chat", "plugins", "box", "settings"]);
    expect(out.railOrder.right).toEqual(["beads"]);
  });

  it("does not duplicate Box if a layout already has it", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "box", "settings"], right: [] },
    }) as { railOrder: { left: string[] } };
    expect(out.railOrder.left).toEqual(["chat", "box", "settings"]);
  });

  // v4→v5 (#1075): "schedule" folded into the Activity surface (a tab), so it's pruned
  // from a persisted railOrder rather than lingering as a dead rail id.
  it("prunes the obsolete 'schedule' rail surface", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "settings"], right: ["beads", "goals", "schedule"] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.right).toEqual(["beads", "goals"]);
    expect(out.railOrder.left).not.toContain("schedule");
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
  const seed = (left: string[], right: string[]) =>
    useUI.setState({ railOrder: { left, right } });

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
