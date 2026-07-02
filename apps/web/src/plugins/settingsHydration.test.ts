import { describe, expect, it } from "vitest";

import { pluginSchemaNeedsRefetch } from "./settingsHydration";
import type { SettingsGroup } from "../lib/types";

// #1643 — the Configure dialog's open-time hydration guard: refetch the settings
// schema exactly when a CACHED schema lacks the plugin's group (a fresh install the
// cache predates); never when there's no cache (the suspense query fetches anyway)
// or when the group is already present (the schema GET is a gateway round-trip).

const group = (over: Partial<SettingsGroup>): SettingsGroup => ({
  section: "Section",
  fields: [],
  ...over,
});

describe("pluginSchemaNeedsRefetch", () => {
  it("no cached schema → no forced refetch (the mounting query fetches fresh)", () => {
    expect(pluginSchemaNeedsRefetch(undefined, "widgets")).toBe(false);
  });

  it("cached schema already carries the plugin's group → cache is good", () => {
    const cached = { groups: [group({ section: "Widgets", category: "Plugins", plugin_id: "widgets" })] };
    expect(pluginSchemaNeedsRefetch(cached, "widgets")).toBe(false);
  });

  it("cached schema lacks the plugin's group (stale, pre-install) → refetch", () => {
    const cached = {
      groups: [
        group({ section: "Model", category: "Model" }), // core group, no plugin_id
        group({ section: "Other Plugin", category: "Plugins", plugin_id: "other" }),
      ],
    };
    expect(pluginSchemaNeedsRefetch(cached, "widgets")).toBe(true);
  });

  it("cached schema with no plugin groups at all → refetch", () => {
    const cached = { groups: [group({ section: "Model", category: "Model" })] };
    expect(pluginSchemaNeedsRefetch(cached, "widgets")).toBe(true);
  });

  it("empty cached schema → refetch", () => {
    expect(pluginSchemaNeedsRefetch({ groups: [] }, "widgets")).toBe(true);
  });
});
