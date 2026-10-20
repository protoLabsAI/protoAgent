import { describe, it, expect } from "vitest";

import { namedSoul, personaSoul } from "./persona";
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
    const bundle = { ...arch("project-manager-archetype", ""), bundle: "https://example/x" };
    expect(personaSoul(bundle, LIST)).toBe("# Base persona");
  });

  it("treats a whitespace-only soul as empty and falls back", () => {
    expect(personaSoul(arch("x", "   \n  "), LIST)).toBe("# Base persona");
  });

  it("returns empty string when there is no soul and no basic archetype to borrow from", () => {
    expect(personaSoul(arch("x", ""), [arch("custom", "# c")])).toBe("");
  });
});

describe("namedSoul", () => {
  const base = "# Identity\n\nI am protoAgent — a general-purpose operator agent.";

  it("introduces the agent by the name chosen in the wizard", () => {
    expect(namedSoul(base, " scout ", base)).toBe("# Identity\n\nI am scout — a general-purpose operator agent.");
  });

  it("leaves the SOUL alone for the default name, a blank name, or a custom persona", () => {
    expect(namedSoul(base, "protoagent", base)).toBe(base);
    expect(namedSoul(base, "", base)).toBe(base);
    expect(namedSoul("# Mine\nI am Ada.", "scout", "# Mine\nI am Ada.")).toBe("# Mine\nI am Ada.");
  });

  it("never rewrites a SOUL the operator edited, even if the phrase survives in it", () => {
    const edited = "# Identity\n\nI am Ada.\n\nNote: never say \"I am protoAgent\".";
    expect(namedSoul(edited, "scout", base)).toBe(edited);
  });
});
