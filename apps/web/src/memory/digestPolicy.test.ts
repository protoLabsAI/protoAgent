import { describe, expect, it } from "vitest";

import { DIGEST_POLICY_HINT, sessionBadge } from "./digestPolicy";

describe("sessionBadge", () => {
  it("badges the viewed chat as itself, never as aged out", () => {
    // Its in_digest is false BY DESIGN (a session is not its own prior session),
    // so the warning badge would be telling the operator the wrong story.
    const badge = sessionBadge({ in_digest: false, is_active_session: true }, "newest");
    expect(badge).toMatchObject({ tone: "neutral", label: "this chat" });
  });

  it("draws nothing for a row that is in the digest", () => {
    expect(sessionBadge({ in_digest: true }, "newest")).toBeNull();
  });

  it("draws nothing when in_digest is unknown (the relevant policy, or an old backend)", () => {
    // Absent must not read as excluded: `relevant` re-chooses the digest per turn.
    expect(sessionBadge({}, "relevant")).toBeNull();
  });

  it("drops the per-row warning under off — the hint says it once", () => {
    // Every row is equally not-injected there, so the badge would distinguish
    // nothing while implying this row in particular aged out.
    expect(sessionBadge({ in_digest: false }, "off")).toBeNull();
    expect(DIGEST_POLICY_HINT.off).toMatch(/none of these are injected/);
    expect(DIGEST_POLICY_HINT.off).toMatch(/session_search/);
  });

  it("still names the viewed chat under off", () => {
    expect(sessionBadge({ in_digest: false, is_active_session: true }, "off")?.label).toBe("this chat");
  });

  it("keeps the window story only for newest", () => {
    expect(sessionBadge({ in_digest: false }, "newest")?.title).toMatch(/digest window/);
    expect(DIGEST_POLICY_HINT.relevant).toMatch(/matching what was just asked/);
  });
});
