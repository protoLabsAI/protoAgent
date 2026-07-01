import { describe, it, expect } from "vitest";

import { personaSoul } from "./persona";
import type { Archetype } from "../lib/types";

const arch = (id: string, soul: string): Archetype => ({
  id,
  label: id,
  icon: "Package",
  blurb: "",
  bundle: null,
  soul,
});

const LIST: Archetype[] = [arch("basic", "# Base persona"), arch("custom", "# Fill me in")];

describe("personaSoul", () => {
  it("returns the archetype's own soul when it has one", () => {
    expect(personaSoul(arch("basic", "# Base persona"), LIST)).toBe("# Base persona");
  });

  it("falls back to the basic archetype's soul for a bundle archetype with no inline persona", () => {
    // A bundle archetype whose manifest omits `soul:` — must not blank the editor.
    const bundle = { ...arch("product-stack", ""), bundle: "https://example/x" };
    expect(personaSoul(bundle, LIST)).toBe("# Base persona");
  });

  it("treats a whitespace-only soul as empty and falls back", () => {
    expect(personaSoul(arch("x", "   \n  "), LIST)).toBe("# Base persona");
  });

  it("returns empty string when there is no soul and no basic archetype to borrow from", () => {
    expect(personaSoul(arch("x", ""), [arch("custom", "# c")])).toBe("");
  });
});
