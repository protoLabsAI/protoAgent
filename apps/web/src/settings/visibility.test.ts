import { describe, expect, it } from "vitest";

import type { SettingsField } from "../lib/types";
import { fieldVisible } from "./visibility";

// A minimal field factory — only the bits fieldVisible reads.
const field = (depends_on?: SettingsField["depends_on"]): SettingsField =>
  ({ key: "child", label: "Child", type: "text", section: "S", restart: false, options: [], scope: "agent", source: "agent", depends_on }) as SettingsField;

describe("fieldVisible (#963 depends_on)", () => {
  it("shows a field with no depends_on", () => {
    expect(fieldVisible(field(), () => undefined)).toBe(true);
  });

  it("{equals}: shows only on strict equality", () => {
    const f = field({ key: "ask_enabled", equals: true });
    expect(fieldVisible(f, () => true)).toBe(true);
    expect(fieldVisible(f, () => false)).toBe(false);
    expect(fieldVisible(f, () => undefined)).toBe(false); // unset prerequisite → hidden
  });

  it("{equals} a string value", () => {
    const f = field({ key: "mode", equals: "advanced" });
    expect(fieldVisible(f, () => "advanced")).toBe(true);
    expect(fieldVisible(f, () => "basic")).toBe(false);
  });

  it("{in}: shows on membership", () => {
    const f = field({ key: "mode", in: ["a", "b"] });
    expect(fieldVisible(f, () => "b")).toBe(true);
    expect(fieldVisible(f, () => "c")).toBe(false);
  });

  it("bare {key}: shows once the prerequisite is truthy", () => {
    const f = field({ key: "ask_enabled" });
    expect(fieldVisible(f, () => true)).toBe(true);
    expect(fieldVisible(f, () => "non-empty")).toBe(true);
    expect(fieldVisible(f, () => false)).toBe(false);
    expect(fieldVisible(f, () => "")).toBe(false);
  });

  it("reads the value of the named sibling key, not its own", () => {
    const f = field({ key: "ask_enabled", equals: true });
    const values: Record<string, unknown> = { ask_enabled: true, child: false };
    expect(fieldVisible(f, (k) => values[k])).toBe(true);
  });
});
