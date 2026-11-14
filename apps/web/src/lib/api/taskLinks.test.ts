import { describe, expect, it } from "vitest";
import { normalizeTaskLinks } from "./automation";

describe("normalizeTaskLinks", () => {
  it("decodes the JSON string the server stores links as", () => {
    const task = normalizeTaskLinks({ id: "t", links: '[{"ref": "https://x/1", "kind": "pr"}, {"kind": "no-ref"}]' });
    expect(task.links).toEqual([{ ref: "https://x/1", kind: "pr" }]);
  });
  it("keeps an array and drops entries without a ref", () => {
    expect(normalizeTaskLinks({ links: [{ ref: "a" }, null, { x: 1 }] }).links).toEqual([{ ref: "a" }]);
  });
  it("turns empty, malformed or missing links into an empty array", () => {
    expect(normalizeTaskLinks({ links: "" }).links).toEqual([]);
    expect(normalizeTaskLinks({ links: "{not json" }).links).toEqual([]);
    expect(normalizeTaskLinks({ links: '{"ref": "x"}' }).links).toEqual([]);
    expect(normalizeTaskLinks({}).links).toEqual([]);
  });
});
