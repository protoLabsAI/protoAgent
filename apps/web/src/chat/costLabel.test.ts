// #2463 — subscription-backed turns must never present the pricing-table
// estimate as a charge; API-key turns keep the plain cost wording.
import { describe, expect, it } from "vitest";

import {
  costAriaLabel,
  costPrefix,
  costTipLabel,
  costTipSub,
  isSubscriptionProvider,
  usageTipNote,
} from "./costLabel";

describe("isSubscriptionProvider", () => {
  it("matches the native OAuth providers, case/space-insensitively", () => {
    expect(isSubscriptionProvider("openai-codex")).toBe(true);
    expect(isSubscriptionProvider("anthropic-oauth")).toBe(true);
    expect(isSubscriptionProvider(" OpenAI-Codex ")).toBe(true);
  });

  it("treats gateway/API-key providers (and absence) as non-subscription", () => {
    expect(isSubscriptionProvider("openai")).toBe(false);
    expect(isSubscriptionProvider("")).toBe(false);
    expect(isSubscriptionProvider(undefined)).toBe(false);
  });
});

describe("subscription cost wording", () => {
  it("marks subscription dollars as an estimate everywhere they render", () => {
    expect(costPrefix(true)).toBe("~");
    expect(costTipLabel(true)).toBe("Est. cost");
    expect(costTipSub(true)).toContain("not an additional charge");
    expect(costAriaLabel(true)).toContain("not a charge");
    expect(usageTipNote(true)).toContain("nothing extra was charged");
  });

  it("keeps plain cost wording for API-key turns", () => {
    expect(costPrefix(false)).toBe("");
    expect(costTipLabel(false)).toBe("Cost");
    expect(costTipSub(false)).toBeUndefined();
    expect(costAriaLabel(false)).toBe("cost");
    expect(usageTipNote(false)).toContain("summed across the turn's calls");
  });
});
