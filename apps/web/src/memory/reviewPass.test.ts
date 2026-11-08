// Review pass (ADR 0125 P3) api-client contract: the filter chips must become the right
// query string, and keep / edit / forget / reject / revert / promote must each hit their
// route with the right method + body. These are the wire shapes the P3-D1 routes expect, so
// a drift here is a 404/400 in the console before any e2e runs.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";

describe("review-pass api client", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ enabled: true }) }));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const url = (i = 0) => String(fetchMock.mock.calls[i][0]);
  const init = (i = 0) => fetchMock.mock.calls[i][1] as { method?: string; body?: string };
  const bodyOf = (i = 0) => JSON.parse(String(init(i).body));

  it("maps the filter chips + paging onto the memories listing query string", async () => {
    await api.memoryMemories({ reviewState: "pending", limit: 50, offset: 0 });
    const u = new URL(url(), "http://x");
    expect(u.pathname).toBe("/api/memory/memories");
    expect(u.searchParams.get("review_state")).toBe("pending");
    expect(u.searchParams.get("limit")).toBe("50");
    expect(u.searchParams.get("offset")).toBe("0");
    // Omitted filters must not appear — an empty kind/provenance means "all rows".
    expect(u.searchParams.has("kind")).toBe(false);
    expect(u.searchParams.has("provenance")).toBe(false);
    expect(u.searchParams.has("include_superseded")).toBe(false);
  });

  it("adds kind / provenance / legacy_domain / include_superseded when set", async () => {
    await api.memoryMemories({
      kind: "fact",
      reviewState: "confirmed",
      provenance: "extracted",
      legacyDomain: "preferences",
      includeSuperseded: true,
    });
    const u = new URL(url(), "http://x");
    expect(u.searchParams.get("kind")).toBe("fact");
    expect(u.searchParams.get("review_state")).toBe("confirmed");
    expect(u.searchParams.get("provenance")).toBe("extracted");
    expect(u.searchParams.get("legacy_domain")).toBe("preferences");
    expect(u.searchParams.get("include_superseded")).toBe("true");
  });

  it("no filters at all → a bare /api/memory/memories (no '?')", async () => {
    await api.memoryMemories();
    expect(url()).toMatch(/\/api\/memory\/memories$/);
  });

  it("Keep / Reject post the verdict to the memories review route by integer id", async () => {
    await api.memoryReview(201, "confirmed");
    expect(url()).toContain("/api/memory/memories/201/review");
    expect(init().method).toBe("POST");
    expect(bodyOf()).toEqual({ state: "confirmed" });
  });

  it("Edit PATCHes the row with the edited fields", async () => {
    await api.editMemory(201, { content: "Eastern", subject: "time zone", kind: "profile" });
    expect(url()).toContain("/api/memory/memories/201");
    expect(init().method).toBe("PATCH");
    expect(bodyOf()).toEqual({ content: "Eastern", subject: "time zone", kind: "profile" });
  });

  it("Forget DELETEs the row", async () => {
    await api.deleteMemory(205);
    expect(url()).toContain("/api/memory/memories/205");
    expect(init().method).toBe("DELETE");
  });

  it("Revert POSTs to the revert route", async () => {
    await api.revertMemory(204);
    expect(url()).toContain("/api/memory/memories/204/revert");
    expect(init().method).toBe("POST");
  });

  it("Promote POSTs to the promote route; candidates is a plain GET", async () => {
    await api.promoteMemory(301);
    expect(url()).toContain("/api/memory/memories/301/promote");
    expect(init().method).toBe("POST");

    fetchMock.mockClear();
    await api.memoryProfileCandidates();
    expect(url()).toContain("/api/memory/profile-candidates");
    expect(init().method).toBeUndefined(); // GET — no method override
  });
});
