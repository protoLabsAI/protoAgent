import { describe, it, expect } from "vitest";

import { mergeTheme, normalizeThemeBlob, resolveThemeToPersist } from "./themeMerge";

// #1762 — the console persists a `{mode, overrides}` theme blob; on boot the user's
// persisted overrides must WIN over the agent/server default (defaults only fill gaps),
// and a change must produce the merged result — not reset to the default object.

const DEFAULT = { mode: "dark" as const, overrides: { "--pl-color-accent": "#9b87f2", "--pl-radius": "6px" } };

describe("normalizeThemeBlob — defensive coercion", () => {
  it("keeps a valid {mode, overrides} blob", () => {
    expect(normalizeThemeBlob({ mode: "light", overrides: { "--pl-color-accent": "#f00" } })).toEqual({
      mode: "light",
      overrides: { "--pl-color-accent": "#f00" },
    });
  });

  it("drops non-`--pl-*` and non-string override tokens (no arbitrary CSS var injection)", () => {
    expect(
      normalizeThemeBlob({
        mode: "dark",
        overrides: { "--pl-color-accent": "#f00", "--evil": "url(x)", "--pl-radius": 6 },
      }),
    ).toEqual({ mode: "dark", overrides: { "--pl-color-accent": "#f00" } });
  });

  it("coerces an invalid mode to undefined (falls back to the design default downstream)", () => {
    expect(normalizeThemeBlob({ mode: "neon", overrides: { "--pl-color-accent": "#f00" } })).toEqual({
      overrides: { "--pl-color-accent": "#f00" },
    });
  });

  it("returns null for empty / non-object / array input (→ design-system defaults)", () => {
    expect(normalizeThemeBlob(null)).toBeNull();
    expect(normalizeThemeBlob(undefined)).toBeNull();
    expect(normalizeThemeBlob({})).toBeNull();
    expect(normalizeThemeBlob({ overrides: {} })).toBeNull();
    expect(normalizeThemeBlob([1, 2])).toBeNull();
    expect(normalizeThemeBlob("dark")).toBeNull();
  });

  it("preserves unknown top-level keys (forward-compat with future DS token groups)", () => {
    expect(normalizeThemeBlob({ mode: "dark", fontSize: "lg", overrides: {} })).toEqual({
      mode: "dark",
      fontSize: "lg",
      overrides: {},
    });
  });
});

describe("mergeTheme — user overrides WIN, defaults fill the gaps (#1762)", () => {
  it("an override beats the default for the same token", () => {
    const merged = mergeTheme(DEFAULT, { mode: "dark", overrides: { "--pl-color-accent": "#00ff00" } });
    expect(merged?.overrides?.["--pl-color-accent"]).toBe("#00ff00"); // user wins
  });

  it("an absent override falls back to the default", () => {
    const merged = mergeTheme(DEFAULT, { mode: "dark", overrides: { "--pl-color-accent": "#00ff00" } });
    expect(merged?.overrides?.["--pl-radius"]).toBe("6px"); // default fills the gap
  });

  it("the user's mode wins over the default's", () => {
    expect(mergeTheme(DEFAULT, { mode: "light", overrides: {} })?.mode).toBe("light");
  });

  it("falls back to the default's mode when the user set none", () => {
    expect(mergeTheme(DEFAULT, { overrides: { "--pl-color-accent": "#00ff00" } })?.mode).toBe("dark");
  });

  it("no user blob → the default is applied unchanged (fresh install)", () => {
    expect(mergeTheme(DEFAULT, null)).toEqual(DEFAULT);
  });

  it("no default → the user's persisted overrides survive (unsaved tweak across reload)", () => {
    const user = { mode: "light" as const, overrides: { "--pl-color-accent": "#00ff00" } };
    expect(mergeTheme(null, user)).toEqual(user);
  });

  it("both empty → null (design-system defaults, nothing stamped)", () => {
    expect(mergeTheme(null, null)).toBeNull();
    expect(mergeTheme({}, {})).toBeNull();
  });

  it("does not mutate its inputs", () => {
    const d = { mode: "dark" as const, overrides: { "--pl-radius": "6px" } };
    const u = { mode: "light" as const, overrides: { "--pl-color-accent": "#0f0" } };
    mergeTheme(d, u);
    expect(d).toEqual({ mode: "dark", overrides: { "--pl-radius": "6px" } });
    expect(u).toEqual({ mode: "light", overrides: { "--pl-color-accent": "#0f0" } });
  });
});

describe("mergeTheme — theme families (DS `preset`)", () => {
  const amberDark = {
    mode: "dark" as const,
    preset: "amber",
    overrides: { "--pl-color-accent": "oklch(0.77 0.16 65)", "--pl-color-status-warning": "oklch(0.88 0.15 100)" },
  };

  it("a different family in the working copy wins wholesale — no tokens leak in from the default's family", () => {
    const steelLight = { mode: "light" as const, preset: "steel", overrides: { "--pl-color-bg": "oklch(0.975 0.002 286)" } };
    expect(mergeTheme(amberDark, steelLight)).toEqual(steelLight);
  });

  it("a family over a hand-tuned default (no preset) wins wholesale too", () => {
    const handTuned = { mode: "dark" as const, overrides: { "--pl-color-accent": "#d72b43", "--pl-radius": "2px" } };
    const nord = { mode: "dark" as const, preset: "nord", overrides: { "--pl-color-bg": "#2e3440" } };
    expect(mergeTheme(handTuned, nord)).toEqual(nord);
  });

  it("a wholesale family blob with no mode takes the default's mode", () => {
    const noMode = { preset: "steel", overrides: { "--pl-color-bg": "oklch(0.141 0.005 286)" } };
    expect(mergeTheme(amberDark, noMode)).toEqual({ ...noMode, mode: "dark" });
  });

  it("the same family keeps the per-token merge — the user's edit wins, the default fills gaps", () => {
    const edited = { mode: "dark" as const, preset: "amber", overrides: { "--pl-color-accent": "#123456" } };
    expect(mergeTheme(amberDark, edited)).toEqual({
      mode: "dark",
      preset: "amber",
      overrides: { "--pl-color-accent": "#123456", "--pl-color-status-warning": "oklch(0.88 0.15 100)" },
    });
  });

  it("a working copy with NO family over a family default wins wholesale (a saved preset, an import, a reset look)", () => {
    const savedLook = { mode: "dark" as const, saved: "user-mine", overrides: { "--pl-color-accent": "#ff8800" } };
    expect(mergeTheme(amberDark, savedLook)).toEqual(savedLook);
  });

  it("with no family on either side, the original per-token merge still applies (#1762)", () => {
    const handTuned = { mode: "dark" as const, overrides: { "--pl-color-accent": "#d72b43", "--pl-radius": "2px" } };
    const tweak = { mode: "dark" as const, overrides: { "--pl-radius": "8px" } };
    expect(mergeTheme(handTuned, tweak)).toEqual({ mode: "dark", overrides: { "--pl-color-accent": "#d72b43", "--pl-radius": "8px" } });
  });

  it("same family: edits keep the user's, plus the default's edits to tokens the user didn't set", () => {
    const def = { ...amberDark, edits: ["--pl-color-accent-fg", "--pl-color-focus"], overrides: { ...amberDark.overrides, "--pl-color-accent-fg": "#123456", "--pl-color-focus": "#654321" } };
    const user = { mode: "dark" as const, preset: "amber", edits: ["--pl-radius"], overrides: { "--pl-color-focus": "oklch(0.77 0.16 65)", "--pl-radius": "8px" } };
    const merged = mergeTheme(def, user)!;
    // focus: the user set it (the family's own value) without listing it → not an edit.
    expect(merged.edits).toEqual(["--pl-radius", "--pl-color-accent-fg"]);
    expect(merged.overrides?.["--pl-color-accent-fg"]).toBe("#123456");
  });

  it("same family: a 0.61 working copy (no edits list) leaves edits unset — never the default's list", () => {
    const def = { ...amberDark, edits: ["--pl-color-accent"], overrides: { ...amberDark.overrides, "--pl-color-accent": "#123456" } };
    const legacy = { mode: "dark" as const, preset: "amber", overrides: { ...amberDark.overrides } };
    const merged = mergeTheme(def, legacy)!;
    expect(merged).not.toHaveProperty("edits");
    expect(merged.overrides?.["--pl-color-accent"]).toBe(amberDark.overrides["--pl-color-accent"]);
  });

  it("`saved` is never inherited, with or without a family", () => {
    const def = { mode: "dark" as const, saved: "user-mine", overrides: { "--pl-color-accent": "#ff8800" } };
    const user = { mode: "dark" as const, overrides: { "--pl-color-accent": "#00ff88" } };
    expect(mergeTheme(def, user)).not.toHaveProperty("saved");
  });

  it("same family: `saved` is never inherited from the default", () => {
    const def = { ...amberDark, saved: "user-mine", edits: [] };
    const user = { mode: "dark" as const, preset: "amber", edits: [], overrides: { "--pl-radius": "8px" } };
    expect(mergeTheme(def, user)).not.toHaveProperty("saved");
  });
});

describe("resolveThemeToPersist — boot reads persisted state, switch adopts the incoming theme", () => {
  const persisted = { mode: "light" as const, overrides: { "--pl-color-accent": "#00ff00" } };

  it("boot (preservePersisted): the persisted user override wins over the incoming default", () => {
    const out = resolveThemeToPersist(DEFAULT, persisted, { preservePersisted: true });
    expect(out?.mode).toBe("light"); // user's persisted mode, not the default's dark
    expect(out?.overrides?.["--pl-color-accent"]).toBe("#00ff00"); // user's accent
    expect(out?.overrides?.["--pl-radius"]).toBe("6px"); // default fills the gap
  });

  it("boot with no server default: the persisted overrides are kept (not clobbered)", () => {
    expect(resolveThemeToPersist(null, persisted, { preservePersisted: true })).toEqual(persisted);
  });

  it("switch/reset (default): the incoming theme replaces, ignoring the persisted copy (ADR 0042)", () => {
    const out = resolveThemeToPersist(DEFAULT, persisted);
    expect(out).toEqual(DEFAULT); // agent B's saved look wins on an explicit switch
  });

  it("switch to an agent with no theme → null (repaints to design-system defaults)", () => {
    expect(resolveThemeToPersist(null, persisted)).toBeNull();
  });
});
