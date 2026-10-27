import { describe, expect, it } from "vitest";

// #4534 (protoContent ds-tc6) — the touch-tap fix for DropdownSelect/Menu ships purely in the
// design system: @protolabsai/ui is floored at 0.68.5, the release that opens the trigger on
// `click` instead of `pointerdown` for touch pointers. There is no console-side code for it.
//
// ui@0.68.5 declares a PINNED dependency on @protolabsai/design 0.14.0 (its components are built
// against those tokens), so the top-level @protolabsai/design the app loads at runtime
// (main.tsx imports "@protolabsai/design/css/tokens") must resolve to 0.14.0 as well. If the
// top-level design floor lagged — e.g. stayed at ^0.13.0 while ui pulled design 0.14.0 — the app
// would paint 0.13.x tokens under 0.14.0-built components and any token value that moved in
// 0.14.0 becomes a silent visual regression. This guard is what fails loudly if that split
// reopens. ui and ui-css are one changesets `fixed` group, so they must stay on the same version.
// JSON module import (resolveJsonModule) rather than node:fs: this tsconfig has no node types, and
// under vitest+jsdom `import.meta.url` is an http: URL (see uiDependencyFloor.test.ts).
import pkg from "../package.json";

const deps = (pkg as { dependencies: Record<string, string> }).dependencies;

// The version of @protolabsai/design that @protolabsai/ui@0.68.5 pins (from the committed
// package-lock.json: node_modules/@protolabsai/ui -> dependencies["@protolabsai/design"]).
const DESIGN_FLOOR: readonly [number, number, number] = [0, 14, 0];
// The release that carries protoContent ds-tc6 (touch opens on click).
const UI_TOUCH_TAP_FLOOR: readonly [number, number, number] = [0, 68, 5];

// The lowest version a `^` / `~` / exact npm range can resolve to is its base version with the
// operator stripped — caret and tilde only widen the ceiling, they never lower the floor.
function minVersion(range: string): [number, number, number] {
  const base = range.replace(/^[\^~]/, "").trim();
  const parts = base.split(".");
  expect(parts).toHaveLength(3);
  const nums = parts.map((part) => Number.parseInt(part, 10));
  expect(nums.every((n) => Number.isInteger(n))).toBe(true);
  return [nums[0], nums[1], nums[2]];
}

function gte(a: readonly number[], b: readonly number[]): boolean {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return true;
}

describe("console @protolabsai design/ui version alignment (#4534, ds-tc6)", () => {
  it("floors @protolabsai/ui at >= 0.68.5 so the touch-tap DropdownSelect/Menu fix ships", () => {
    expect(deps["@protolabsai/ui"]).toBeTruthy();
    expect(gte(minVersion(deps["@protolabsai/ui"]), UI_TOUCH_TAP_FLOOR)).toBe(true);
  });

  it("floors @protolabsai/design at >= 0.14.0 so the app loads the tokens ui is built against", () => {
    expect(deps["@protolabsai/design"]).toBeTruthy();
    expect(gte(minVersion(deps["@protolabsai/design"]), DESIGN_FLOOR)).toBe(true);
  });

  it("pins @protolabsai/ui and @protolabsai/ui-css to the same version (one `fixed` group)", () => {
    expect(deps["@protolabsai/ui-css"]).toBeTruthy();
    expect(deps["@protolabsai/ui-css"]).toBe(deps["@protolabsai/ui"]);
  });

  it("keeps caret ranges so compatible design/ui patches and minors are still accepted", () => {
    for (const name of ["@protolabsai/design", "@protolabsai/ui", "@protolabsai/ui-css"] as const) {
      expect(deps[name].startsWith("^")).toBe(true);
    }
  });
});
