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
      railOrder: { left: ["chat", "knowledge", "box"], right: ["work"] },
    }) as { railOrder: { left: string[]; right: string[] } };
    expect(out.railOrder.left).toEqual(["chat", "knowledge"]);
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

  // (v9 "re-add settings to the rail" is gone: Settings moved to a utility-bar pill in v12,
  // which prunes the "settings" rail id — re-adding it would be undone. See the v12 test.)

  // v10 (2026-06): the Plugins manager moved into Settings ▸ Plugins. Prune "plugins" from
  // every dock + the quick-bar so it doesn't linger as a dead rail id.
  it("prunes 'plugins' from the rails and quick-bar", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "plugins", "knowledge"], right: ["plugins", "work"], bottom: ["plugins"] },
      quickBar: ["chat", "plugins", "knowledge"],
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] }; quickBar: string[] };
    expect(out.railOrder.left).not.toContain("plugins");
    expect(out.railOrder.right).not.toContain("plugins");
    expect(out.railOrder.bottom).not.toContain("plugins");
    expect(out.railOrder.left).toEqual(["chat", "knowledge"]);
    expect(out.quickBar).toEqual(["chat", "knowledge"]);
  });

  // v12 (2026-06): Settings moved off the rail into a utility-bar pill. Prune "settings" from
  // every dock + the quick-bar so it doesn't linger as a dead rail id.
  it("prunes 'settings' from the rails and quick-bar", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "settings", "knowledge"], right: ["settings", "work"], bottom: ["settings"] },
      quickBar: ["chat", "settings", "knowledge"],
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] }; quickBar: string[] };
    expect(out.railOrder.left).not.toContain("settings");
    expect(out.railOrder.right).not.toContain("settings");
    expect(out.railOrder.bottom).not.toContain("settings");
    expect(out.railOrder.left).toEqual(["chat", "knowledge"]);
    expect(out.quickBar).toEqual(["chat", "knowledge"]);
  });

  // v11 (2026-06): Tasks + Goals + Schedule folded into the unified "work" hub.
  it("folds tasks/goals/schedule into the 'work' hub", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "schedule", "knowledge"], right: ["tasks", "goals"], bottom: [] },
      rightPanel: "tasks",
      quickBar: ["chat", "tasks", "knowledge"],
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] }; rightPanel: string; quickBar: string[] };
    for (const id of ["tasks", "goals", "schedule"]) {
      expect(out.railOrder.left).not.toContain(id);
      expect(out.railOrder.right).not.toContain(id);
      expect(out.quickBar).not.toContain(id);
    }
    expect(out.railOrder.left).toEqual(["chat", "knowledge"]);
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
      railOrder: { left: ["chat", "knowledge"], right: ["work"] },
    }) as { railOrder: { left: string[]; right: string[]; bottom: string[] } };
    expect(out.railOrder.bottom).toEqual([]);
    expect(out.railOrder.left).toEqual(["chat", "knowledge"]);
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
  const seed = (left: string[], right: string[], bottom: string[] = [], hidden: string[] = []) =>
    useUI.setState({ railOrder: { left, right, bottom, hidden } });

  beforeEach(() => seed(["chat", "plugin:doom:panel"], ["tasks", "plugin:board:board", "notes"]));

  it("keeps a moved view at its persisted side and position despite its declared side", () => {
    // board's manifest says right→ but suppose the operator dragged doom to the left
    // already; both views re-declare their manifest sides on every reconcile.
    useUI.getState().reconcilePluginViews([
      { id: "plugin:doom:panel", side: "right" }, // manifest says right; operator put it LEFT
      { id: "plugin:board:board", side: "right" },
    ]);
    expect(useUI.getState().railOrder.left).toEqual(["chat", "plugin:doom:panel"]);
    expect(useUI.getState().railOrder.right).toEqual(["tasks", "plugin:board:board", "notes"]);
  });

  it("keeps mid-rail positions (no prune/re-append shuffle)", () => {
    useUI.getState().reconcilePluginViews([{ id: "plugin:board:board", side: "right" }, { id: "plugin:doom:panel", side: "left" }]);
    // board stays BETWEEN tasks and notes — not re-appended to the bottom.
    expect(useUI.getState().railOrder.right).toEqual(["tasks", "plugin:board:board", "notes"]);
  });

  it("appends a NEW view at its declared side", () => {
    useUI.getState().reconcilePluginViews([
      { id: "plugin:doom:panel", side: "left" },
      { id: "plugin:board:board", side: "right" },
      { id: "plugin:browser:panel", side: "right" },
    ]);
    expect(useUI.getState().railOrder.right).toEqual(["tasks", "plugin:board:board", "notes", "plugin:browser:panel"]);
  });

  it("prunes a view absent from a non-empty set, leaving core surfaces alone", () => {
    useUI.getState().reconcilePluginViews([{ id: "plugin:doom:panel", side: "left" }]);
    expect(useUI.getState().railOrder.left).toEqual(["chat", "plugin:doom:panel"]);
    expect(useUI.getState().railOrder.right).toEqual(["tasks", "notes"]);
  });

  it("prunes everything on an empty set — why the caller must gate on loaded", () => {
    useUI.getState().reconcilePluginViews([]);
    expect(useUI.getState().railOrder.left).toEqual(["chat"]);
    expect(useUI.getState().railOrder.right).toEqual(["tasks", "notes"]);
  });
});

// The general safety net for CORE surfaces (Knowledge regression, 2026-06): railSurfaces()
// only renders ids already in a persisted railOrder and never re-adds a missing core surface,
// so a layout saved before a surface existed silently drops its icon. reconcileCoreSurfaces
// restores it on its default dock — replacing the per-surface v9-style migrations.
describe("reconcileCoreSurfaces", () => {
  const CORE = ["chat", "work", "knowledge"];

  it("re-adds a CORE surface missing from a persisted railOrder to its default dock", () => {
    useUI.setState({ railOrder: { left: ["chat", "plugins"], right: ["work"], bottom: [], hidden: [] } });
    useUI.getState().reconcileCoreSurfaces(CORE);
    expect(useUI.getState().railOrder.left).toContain("knowledge"); // restored on its default (left)
  });

  it("is a no-op (same ref, no write) when every core surface is already placed", () => {
    useUI.setState({ railOrder: { left: ["chat", "knowledge"], right: ["work"], bottom: [], hidden: [] } });
    const before = useUI.getState().railOrder;
    useUI.getState().reconcileCoreSurfaces(CORE);
    expect(useUI.getState().railOrder).toBe(before);
  });

  it("respects a surface the operator moved to another dock — no duplicate", () => {
    useUI.setState({ railOrder: { left: ["chat"], right: ["work", "knowledge"], bottom: [], hidden: [] } });
    useUI.getState().reconcileCoreSurfaces(CORE);
    expect(useUI.getState().railOrder.left).not.toContain("knowledge");
    expect(useUI.getState().railOrder.right).toContain("knowledge"); // left where the operator put it
  });

  it("does NOT re-add a core surface the operator hid (hidden counts as placed)", () => {
    useUI.setState({ railOrder: { left: ["chat"], right: ["work"], bottom: [], hidden: ["knowledge"] } });
    useUI.getState().reconcileCoreSurfaces(CORE);
    expect(useUI.getState().railOrder.left).not.toContain("knowledge");
    expect(useUI.getState().railOrder.hidden).toEqual(["knowledge"]); // stays hidden, not resurrected
  });
});

// hideSurface / showSurface — the "hidden but enabled" bucket (ADR 0035/0036). A surface is on
// exactly one dock OR in `hidden`; hiding removes its rail icon without disabling the plugin, and
// showing restores it (to its core default dock, else left). The reconcilers respect `hidden` so a
// reload never resurrects a hidden view; uninstalling the plugin prunes it from `hidden`.
describe("hideSurface / showSurface", () => {
  const seed = (left: string[], right: string[], bottom: string[] = [], hidden: string[] = []) =>
    useUI.setState({ railOrder: { left, right, bottom, hidden } });

  it("hides a surface off its dock into the hidden bucket", () => {
    seed(["chat", "knowledge"], ["work"]);
    useUI.getState().hideSurface("knowledge");
    expect(useUI.getState().railOrder.left).toEqual(["chat"]);
    expect(useUI.getState().railOrder.hidden).toEqual(["knowledge"]);
  });

  it("hiding is idempotent — re-hiding a hidden id is a no-op (same ref)", () => {
    seed(["chat"], ["work"], [], ["knowledge"]);
    const before = useUI.getState().railOrder;
    useUI.getState().hideSurface("knowledge");
    expect(useUI.getState().railOrder).toBe(before);
  });

  it("shows a hidden core surface back on its default dock", () => {
    seed(["chat"], [], [], ["work", "knowledge"]);
    useUI.getState().showSurface("work"); // work's core default is the right dock
    expect(useUI.getState().railOrder.right).toContain("work");
    expect(useUI.getState().railOrder.hidden).toEqual(["knowledge"]);
  });

  it("shows a hidden plugin view on the left rail by default (no known dock)", () => {
    seed(["chat"], ["work"], [], ["plugin:board:board"]);
    useUI.getState().showSurface("plugin:board:board");
    expect(useUI.getState().railOrder.left).toContain("plugin:board:board");
    expect(useUI.getState().railOrder.hidden).toEqual([]);
  });

  it("shows onto an explicit dock when asked", () => {
    seed(["chat"], ["work"], [], ["plugin:board:board"]);
    useUI.getState().showSurface("plugin:board:board", "right");
    expect(useUI.getState().railOrder.right).toEqual(["work", "plugin:board:board"]);
  });

  it("moveSurface un-hides (move doubles as restore)", () => {
    seed(["chat"], ["work"], [], ["plugin:board:board"]);
    useUI.getState().moveSurface("plugin:board:board", "bottom");
    expect(useUI.getState().railOrder.hidden).toEqual([]);
    expect(useUI.getState().railOrder.bottom).toEqual(["plugin:board:board"]);
  });

  it("reconcilePluginViews keeps a hidden view hidden and never re-docks it", () => {
    seed(["chat"], ["work"], [], ["plugin:board:board"]);
    // board is still installed (declares its right placement) — but the operator hid it.
    useUI.getState().reconcilePluginViews([{ id: "plugin:board:board", side: "right" }]);
    expect(useUI.getState().railOrder.right).toEqual(["work"]); // NOT resurrected
    expect(useUI.getState().railOrder.hidden).toEqual(["plugin:board:board"]);
  });

  it("reconcilePluginViews prunes a hidden view when its plugin is uninstalled", () => {
    seed(["chat"], ["work"], [], ["plugin:board:board"]);
    useUI.getState().reconcilePluginViews([]); // board gone from the loaded set
    expect(useUI.getState().railOrder.hidden).toEqual([]);
  });
});

// v14 (ADR 0048 ratified — domain-first IA): the dead `settingsScope` "two homes" axis is
// dropped (no view read it), and a persisted default `settingsSection: "overview"` (host-only
// Box section) is retargeted to the new universal default "identity" (the first Agent domain).
describe("migrateUiState — v14 domain-first settings IA", () => {
  it("drops the dead settingsScope axis", () => {
    const out = migrateUiState({ settingsScope: "host", rightWidth: 320 }) as Record<string, unknown>;
    expect(out).not.toHaveProperty("settingsScope");
    expect(out).toEqual({ rightWidth: 320 });
  });

  it("retargets the old 'overview' default section to 'identity'", () => {
    const out = migrateUiState({ settingsSection: "overview" }) as { settingsSection: string };
    expect(out.settingsSection).toBe("identity");
  });

  it("leaves a user-chosen section untouched", () => {
    const out = migrateUiState({ settingsSection: "model" }) as { settingsSection: string };
    expect(out.settingsSection).toBe("model");
  });
});

// The v13 migration adds the `hidden` bucket to a persisted railOrder that predates it, so the
// shape is complete (actions also fall back to [] defensively).
describe("migrateUiState — v13 hidden bucket", () => {
  it("adds an empty hidden array to a railOrder without one", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat", "knowledge"], right: ["work"], bottom: [] },
    }) as { railOrder: { hidden: string[] } };
    expect(out.railOrder.hidden).toEqual([]);
  });

  it("preserves an existing hidden array", () => {
    const out = migrateUiState({
      railOrder: { left: ["chat"], right: ["work"], bottom: [], hidden: ["knowledge"] },
    }) as { railOrder: { hidden: string[] } };
    expect(out.railOrder.hidden).toEqual(["knowledge"]);
  });
});
