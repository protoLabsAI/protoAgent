// #2462 — pins the post-Finish convergence contract: a BLANKET invalidation
// (no query-key filter). A scoped invalidation is exactly the regression that
// left the composer chip and Settings ▸ Model on pre-setup values.
import { describe, expect, it, vi } from "vitest";

import type { QueryClient } from "@tanstack/react-query";

import { invalidateAllAfterSetup } from "./finish";

describe("invalidateAllAfterSetup", () => {
  it("invalidates the entire cache — no filter argument", () => {
    const invalidateQueries = vi.fn().mockResolvedValue(undefined);
    invalidateAllAfterSetup({ invalidateQueries } as unknown as QueryClient);
    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith();
  });
});
