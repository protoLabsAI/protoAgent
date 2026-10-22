import { describe, expect, it } from "vitest";

// Regression guard for #4421 housekeeping: PR #4440 committed three screenshot PNGs under
// apps/web/e2e/evidence/4421/ (before-scrollback.png, scrollback-top.png, scrollback-bottom.png)
// as "evidence". Binary recordings belong in CI/PR artifact storage, not repository source —
// the scrollback spec already re-captures them to testInfo.outputPath() / testInfo.attach() on
// every run. This sweep deletes the committed copies and keeps the evidence tree from regrowing
// binary blobs.
//
// Asserts absence via a compile-time glob rather than node:fs — this tsconfig has no node types
// and under jsdom import.meta.url is an http: URL, so filesystem access is a trap (mirrors
// tailwindDepsRemoved.test.ts). The glob is rooted at apps/web/, so a deleted file simply drops
// out of the result: an empty match is how we assert the PNGs are gone.
const EVIDENCE_BINARIES = import.meta.glob("../e2e/evidence/**/*.{png,jpg,jpeg,webp,gif}", {
  query: "?url",
  import: "default",
  eager: true,
}) as Record<string, string>;

// A sibling glob of the spec that produces the screenshots at runtime — proves the glob base is
// right (this file exists), so an empty EVIDENCE_BINARIES match means "deleted", not "mis-rooted".
const SPECS = import.meta.glob("../e2e/chat-frame-scrollback.spec.ts", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

describe("#4440 scrollback evidence PNGs stay out of source (#4421)", () => {
  it("the glob base resolves (the scrollback spec is present)", () => {
    expect(Object.keys(SPECS)).toEqual(["../e2e/chat-frame-scrollback.spec.ts"]);
  });

  it("no binary screenshot evidence is committed under e2e/evidence", () => {
    expect(Object.keys(EVIDENCE_BINARIES)).toEqual([]);
  });

  it("the specific #4440 screenshots are gone", () => {
    const names = ["before-scrollback.png", "scrollback-top.png", "scrollback-bottom.png"];
    const stillPresent = Object.keys(EVIDENCE_BINARIES).filter((path) =>
      names.some((name) => path.endsWith(`/${name}`)),
    );
    expect(stillPresent).toEqual([]);
  });

  it("the spec still re-captures the shots as run-scoped artifacts (not repo files)", () => {
    const spec = SPECS["../e2e/chat-frame-scrollback.spec.ts"];
    // The repair/diagnostic coverage must keep producing the proof via outputPath()/attach(),
    // so deleting the committed copies loses nothing reviewers rely on.
    expect(spec).toContain("testInfo.outputPath");
    expect(spec).toContain("testInfo.attach");
  });
});
