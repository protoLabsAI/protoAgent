import { expect, expectTypeOf, it } from "vitest";
import { registerWorkSection, sections, type WorkSection } from "./sectionRegistry";
import { workGroups } from "./workGroups";

it("accepts exactly the canonical group ids, optionally during migration", () => {
  expectTypeOf<WorkSection["group"]>().toEqualTypeOf<
    "needs-you" | "doing-now" | "coming-up" | "instructions" | undefined
  >();
  expectTypeOf<NonNullable<WorkSection["group"]>>().toEqualTypeOf<
    ReturnType<typeof workGroups>[number]["id"]
  >();
});

it("keeps legacy and grouped extension renderers compatible with registration and teardown", () => {
  const legacy: WorkSection = { label: "Legacy", render: () => "Legacy detail" };
  const grouped: WorkSection = { label: "Extension", group: "instructions", render: legacy.render };
  const disposeLegacy = registerWorkSection("test-extension", legacy);
  expect(sections.get("test-extension")).toBe(legacy);
  const disposeGrouped = registerWorkSection("test-extension", grouped);
  try {
    disposeLegacy();
    expect(sections.get("test-extension")).toBe(grouped);
    expect(sections.get("test-extension")?.render).toBe(legacy.render);
  } finally {
    disposeGrouped();
  }
  expect(sections.has("test-extension")).toBe(false);
});
