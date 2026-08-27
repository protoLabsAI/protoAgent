import { describe, expect, it } from "vitest";

import type { SettingsGroup } from "../lib/types";
import { pluginSettingsTabs } from "./SettingsCategory";

const group = (section: string, tab?: { id: string; label: string; order: number }): SettingsGroup => ({
  section,
  category: "Plugins",
  plugin_id: "project_board",
  fields: [],
  settings_tab: tab,
});

describe("pluginSettingsTabs", () => {
  it("keeps an unannotated plugin schema in the single Configuration fallback", () => {
    const groups = [group("Project Board")];
    expect(pluginSettingsTabs(groups)).toEqual([
      { id: "__configuration", label: "Configuration", groups },
    ]);
  });

  it("orders declared tabs by manifest order, not field order", () => {
    const review = group("Review", { id: "review", label: "Review & merge", order: 1 });
    const runtime = group("Runtime", { id: "runtime", label: "Runtime", order: 0 });
    expect(pluginSettingsTabs([review, runtime]).map(({ id, label }) => ({ id, label }))).toEqual([
      { id: "runtime", label: "Runtime" },
      { id: "review", label: "Review & merge" },
    ]);
  });

  it("pins unassigned fields to Configuration ahead of declared tabs", () => {
    const fallback = group("General");
    const runtime = group("Runtime", { id: "runtime", label: "Runtime", order: 0 });
    expect(pluginSettingsTabs([runtime, fallback]).map((tab) => tab.id)).toEqual([
      "__configuration",
      "runtime",
    ]);
  });

  it("coalesces multiple groups assigned to the same stable tab id", () => {
    const one = group("Workers", { id: "runtime", label: "Runtime", order: 0 });
    const two = group("Concurrency", { id: "runtime", label: "Runtime", order: 0 });
    expect(pluginSettingsTabs([one, two])).toEqual([
      { id: "runtime", label: "Runtime", groups: [one, two] },
    ]);
  });
});
