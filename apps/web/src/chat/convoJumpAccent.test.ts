import { describe, expect, it } from "vitest";

// ds-5j6 (#4250) — the design system restyled the Conversation "jump to latest" button
// (`.pl-convo__jump` in @protolabsai/ui-css `styles/ai.css`) to a solid accent fill with an
// on-accent glyph and popover elevation, so it reads clearly over message text instead of the
// former grey-bordered control that could blend into the thread. The console ships this purely
// through the dependency range — there is NO console-side CSS for the accent fill — so, exactly
// like uiDependencyFloor.test.ts (#3412), the floor on the DS range is what guards it: this test
// fails loudly if the ui-css / ui ranges are ever floored below the first release that carries the
// restyle (0.68.1), if the required @protolabsai/design peer (0.13.0) is dropped, or if a local
// `.pl-convo__jump` styling override is reintroduced in src that would mask the DS restyle.
//
// The resolved ai.css cannot be asserted from here: under this vitest config node_modules CSS is
// stubbed to "" (only apps/web/src is in `css.include` — see tokenNameGuard.test.ts), and the
// Python CI job runs without node_modules at all. The caret floor is the CI-safe guarantee, since
// the accent fill is a forward change present in every 0.68.1+ ui-css. package.json is read as a
// JSON module and src stylesheets as `?raw` text via a compile-time glob rather than node:fs: this
// tsconfig has no node types and under vitest+jsdom `import.meta.url` is an http: URL, so filesystem
// access is a trap (mirrors uiDependencyFloor.test.ts / dsStaleFallbackDrop1b.test.ts).
import pkg from "../../package.json";

const deps = (pkg as { dependencies: Record<string, string> }).dependencies;

// First published ui-css/ui release carrying the accent-filled jump button (ds-5j6), and the
// @protolabsai/design version it requires as a peer (ui-css 0.68.1 / ui 0.68.1 both pin 0.13.0).
const DS_5J6_FLOOR: readonly [number, number, number] = [0, 68, 1];
const DESIGN_PEER_FLOOR: readonly [number, number, number] = [0, 13, 0];

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

// Every CSS rule in src, with comments stripped, as { selector, body }. Split on `}` so each chunk
// is `selector { declarations`; comment mentions of the class (e.g. work.css's shadow note) are
// removed first so they never register as selectors.
type Rule = { selector: string; body: string };
function rules(css: string): Rule[] {
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Rule[] = [];
  for (const chunk of noComments.split("}")) {
    const brace = chunk.indexOf("{");
    if (brace === -1) continue;
    out.push({ selector: chunk.slice(0, brace).trim(), body: chunk.slice(brace + 1).trim() });
  }
  return out;
}

function propsOf(body: string): string[] {
  return body
    .split(";")
    .map((decl) => decl.split(":")[0].trim())
    .filter(Boolean);
}

// All src stylesheets (glob rooted at src, one dir up from this chat/ file).
const SRC_CSS = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

describe("Conversation jump-to-latest accent fill (ds-5j6)", () => {
  it("floors @protolabsai/ui-css at >= 0.68.1 so the accent fill ships", () => {
    expect(deps["@protolabsai/ui-css"]).toBeTruthy();
    expect(gte(minVersion(deps["@protolabsai/ui-css"]), DS_5J6_FLOOR)).toBe(true);
  });

  it("floors @protolabsai/ui at >= 0.68.1 (the matching release carrying ds-5j6)", () => {
    expect(deps["@protolabsai/ui"]).toBeTruthy();
    expect(gte(minVersion(deps["@protolabsai/ui"]), DS_5J6_FLOOR)).toBe(true);
  });

  it("floors the @protolabsai/design peer at >= 0.13.0 (required by ui / ui-css 0.68.1)", () => {
    expect(deps["@protolabsai/design"]).toBeTruthy();
    expect(gte(minVersion(deps["@protolabsai/design"]), DESIGN_PEER_FLOOR)).toBe(true);
  });

  it("keeps caret ranges so compatible DS patches/minors are still accepted", () => {
    for (const name of ["@protolabsai/ui-css", "@protolabsai/ui", "@protolabsai/design"]) {
      expect(deps[name].startsWith("^")).toBe(true);
    }
  });

  it("adds no local .pl-convo__jump styling override beyond the hitl.css visibility hide", () => {
    const jumpRules = Object.entries(SRC_CSS).flatMap(([file, css]) =>
      rules(css)
        .filter((r) => r.selector.includes(".pl-convo__jump"))
        .map((r) => ({ file, ...r })),
    );
    // The only src rule allowed to target the class is hitl.css hiding it while a form is open; its
    // sole declaration is `visibility` — anything touching background/color/border/box-shadow would
    // fight the DS accent restyle.
    for (const rule of jumpRules) {
      expect(propsOf(rule.body), `unexpected .pl-convo__jump properties in ${rule.file}`).toEqual([
        "visibility",
      ]);
    }
    // And that hide rule must still exist — the class name is unchanged by ds-5j6, so the HITL
    // suppression keeps working.
    expect(jumpRules.some((r) => r.file.endsWith("/hitl.css"))).toBe(true);
  });
});
