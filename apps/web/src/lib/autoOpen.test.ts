import { afterEach, describe, expect, it } from "vitest";

import { MOBILE_QUERY, TABLET_PORTRAIT_QUERY, autoOpenSuppressed, isMobileViewport } from "./autoOpen";

const original = window.matchMedia;
function matching(...queries: string[]) {
  window.matchMedia = ((q: string) => ({ matches: queries.includes(q), media: q })) as never;
}
afterEach(() => {
  window.matchMedia = original;
});

describe("autoOpenSuppressed — one gate for unrequested opens (ADR 0086, #4336 S5)", () => {
  it("suppresses on a phone", () => {
    matching(MOBILE_QUERY);
    expect(isMobileViewport()).toBe(true);
    expect(autoOpenSuppressed()).toBe(true);
  });

  it("suppresses on a touch tablet in portrait, which is NOT the phone shell", () => {
    matching(TABLET_PORTRAIT_QUERY);
    expect(isMobileViewport()).toBe(false);
    expect(autoOpenSuppressed()).toBe(true);
  });

  it("allows on a desktop or a tablet in landscape", () => {
    matching();
    expect(autoOpenSuppressed()).toBe(false);
  });

  it("fails open to 'allowed' when matchMedia throws", () => {
    window.matchMedia = (() => {
      throw new Error("no matchMedia");
    }) as never;
    expect(autoOpenSuppressed()).toBe(false);
  });
});
