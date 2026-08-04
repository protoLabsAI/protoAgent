import { describe, expect, it } from "vitest";

import { NON_CREDENTIAL_KINDS, splitFindings } from "./snapshotReview";

describe("splitFindings", () => {
  it("routes a credential finding to the rotate-it bucket", () => {
    const { credentials, machineLocal } = splitFindings({ "SOUL.md": ["anthropic-key"] });
    expect(credentials).toEqual([{ where: "SOUL.md", kinds: ["anthropic-key"] }]);
    expect(machineLocal).toEqual([]);
  });

  it("routes a home path to the re-point bucket, NOT the credential one", () => {
    // The distinction that matters: filing a scrubbed path under "credentials found" sends
    // an operator hunting an exposure that never happened.
    const { credentials, machineLocal } = splitFindings({ "operator.project_dir": ["home-path"] });
    expect(credentials).toEqual([]);
    expect(machineLocal).toEqual([{ where: "operator.project_dir", kinds: ["home-path"] }]);
  });

  it("splits one location that produced both kinds", () => {
    const { credentials, machineLocal } = splitFindings({ "notes.scratch": ["home-path", "openai-key"] });
    expect(credentials).toEqual([{ where: "notes.scratch", kinds: ["openai-key"] }]);
    expect(machineLocal).toEqual([{ where: "notes.scratch", kinds: ["home-path"] }]);
  });

  it("is stable-ordered so the panel doesn't reshuffle between re-checks", () => {
    const { credentials } = splitFindings({ "z.field": ["jwt"], "a.field": ["jwt"] });
    expect(credentials.map((f) => f.where)).toEqual(["a.field", "z.field"]);
  });

  it("treats an unknown kind as a credential — fail safe, not silent", () => {
    // A detector added server-side that this list hasn't heard of must surface as something
    // worth looking at, never get quietly dropped from both buckets.
    const { credentials } = splitFindings({ "x.y": ["some-new-detector"] });
    expect(credentials).toEqual([{ where: "x.y", kinds: ["some-new-detector"] }]);
  });

  it("handles an empty review", () => {
    expect(splitFindings({})).toEqual({ credentials: [], machineLocal: [] });
  });

  it("documents the set it mirrors", () => {
    // If this fails, `graph/snapshot_op.py::NON_CREDENTIAL_KINDS` probably changed — keep
    // the two in step.
    expect([...NON_CREDENTIAL_KINDS]).toEqual(["home-path"]);
  });
});
