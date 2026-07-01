import { beforeEach, describe, expect, it } from "vitest";

import "./registrations"; // side-effect: registers the core menus (rail-surface, …)
import { resolveMenu } from "./registry";
import { useUI } from "../state/uiStore";

type Item = { id: string; label?: unknown; danger?: boolean; disabled?: boolean };
const ids = (entries: unknown[]) => (entries as Item[]).map((e) => e.id);

// The rail-surface menu's plugin lifecycle affordances (#1521 / #1522): the App-side
// trigger resolves a plugin's version / removable / updatable into `ctx`, and the menu
// turns those into a version header, an "Update available" action, and a destructive
// "Uninstall…" — each gated so an in-tree built-in never offers update/uninstall.
describe("rail-surface plugin lifecycle menu (#1521 / #1522)", () => {
  const baseCtx = { id: "plugin:board:board", side: "left" as const, pluginId: "board", pluginName: "Board" };

  beforeEach(() => {
    // The menu early-returns [] unless the surface id is a tracked member of its dock.
    useUI.setState({ railOrder: { left: ["chat", "plugin:board:board"], right: [], bottom: [], hidden: [] } });
  });

  it("shows version, Update, and Uninstall for a removable, behind plugin", () => {
    const entries = resolveMenu("rail-surface", { ...baseCtx, pluginVersion: "1.2.3", pluginRemovable: true, pluginUpdatable: true });
    const items = entries as Item[];
    expect(ids(items)).toEqual(expect.arrayContaining(["plugin-version", "update", "uninstall"]));
    expect(items.find((i) => i.id === "plugin-version")?.label).toContain("1.2.3");
    expect(items.find((i) => i.id === "uninstall")?.danger).toBe(true);
  });

  it("hides Update when up to date and Uninstall when not removable", () => {
    const got = ids(resolveMenu("rail-surface", { ...baseCtx, pluginVersion: "1.2.3", pluginRemovable: false, pluginUpdatable: false }));
    expect(got).toContain("plugin-version");
    expect(got).not.toContain("update");
    expect(got).not.toContain("uninstall");
  });

  it("never offers Update/Uninstall for an in-tree built-in", () => {
    const got = ids(resolveMenu("rail-surface", { ...baseCtx, pluginBuiltin: true, pluginRemovable: true, pluginUpdatable: true }));
    expect(got).not.toContain("update");
    expect(got).not.toContain("uninstall");
  });

  it("omits the version header when the version is unknown", () => {
    const got = ids(resolveMenu("rail-surface", { ...baseCtx, pluginRemovable: true, pluginUpdatable: false }));
    expect(got).not.toContain("plugin-version");
    expect(got).toContain("uninstall");
  });
});
