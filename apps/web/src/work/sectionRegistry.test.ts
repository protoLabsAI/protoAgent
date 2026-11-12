// @vitest-environment node
import { expect, expectTypeOf, it } from "vitest";
import { registerWorkSection, sections, type WorkSection } from "./sectionRegistry";
import { workGroups } from "./workGroups";

it("requires exactly one of the canonical group ids — no ungrouped registrations (S4c)", () => {
  expectTypeOf<WorkSection["group"]>().toEqualTypeOf<
    "needs-you" | "doing-now" | "coming-up" | "instructions"
  >();
  expectTypeOf<WorkSection["group"]>().toEqualTypeOf<
    ReturnType<typeof workGroups>[number]["id"]
  >();
  // A plugin registration without a group fails the type contract.
  // @ts-expect-error — group is required
  const ungrouped: WorkSection = { label: "Legacy", render: () => "Legacy detail" };
  void ungrouped;
});

it("rejects an ungrouped or unknown-group registration from untyped callers at runtime", () => {
  const render = () => "detail";
  expect(() =>
    registerWorkSection("rogue", { label: "Rogue", render } as unknown as WorkSection),
  ).toThrow(/group/);
  expect(() =>
    registerWorkSection("rogue", { label: "Rogue", group: "misc", render } as unknown as WorkSection),
  ).toThrow(/needs-you, doing-now, coming-up, instructions/);
  // The rejected registration never half-installs.
  expect(sections.has("rogue")).toBe(false);
});

it("keeps grouped extension renderers compatible with registration and teardown", () => {
  const first: WorkSection = { label: "Extension", group: "coming-up", render: () => "Extension detail" };
  const second: WorkSection = { label: "Extension", group: "instructions", render: first.render };
  const disposeFirst = registerWorkSection("test-extension", first);
  expect(sections.get("test-extension")).toBe(first);
  const disposeSecond = registerWorkSection("test-extension", second);
  try {
    disposeFirst();
    expect(sections.get("test-extension")).toBe(second);
    expect(sections.get("test-extension")?.render).toBe(first.render);
  } finally {
    disposeSecond();
  }
  expect(sections.has("test-extension")).toBe(false);
});
