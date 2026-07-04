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
