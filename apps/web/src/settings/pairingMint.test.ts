import { describe, expect, it } from "vitest";
import src from "./DevicesPanel.tsx?raw";

// Regression guard for the third desktop brick (ADR 0087 D6).
//
// `makeReachable` decided whether to mint a token from `localStorage.getItem(...)`. A browser
// holding a stale token made it skip minting while the SERVER had none — then it wrote a
// non-loopback bind, which the boot guard refuses, so the app never started again and could
// only be recovered by hand-editing YAML.
//
// Asserted against source: the failure is a one-expression change that type-checks, passes
// every behavioural test, and only manifests when the browser's belief and the server's
// config disagree — a state no unit test naturally constructs.
describe("makeReachable mint decision", () => {
  it("keys off the server's reported token state", () => {
    expect(src).toContain("unreachable?.authConfigured === true");
  });

  it("does NOT decide from this browser's localStorage", () => {
    // The exact shape of the bug: a localStorage read gating the mint.
    expect(src).not.toMatch(/const\s+previous\s*=\s*window\.localStorage\.getItem/);
    expect(src).not.toMatch(/if\s*\(!previous\)\s*\{/);
  });

  it("still binds the wildcard, so loopback survives for the desktop app", () => {
    expect(src).toContain('"network.bind": "0.0.0.0"');
  });

  it("restores a prior token when the save fails rather than blanking it", () => {
    expect(src).toMatch(/if \(prior\) window\.localStorage\.setItem/);
  });
});
