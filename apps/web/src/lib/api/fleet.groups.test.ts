import { afterEach, describe, expect, it, vi } from "vitest";
import { fleetApi } from "./fleet";

// The switcher group + pin client calls (fleet-switcher spec,
// docs/design/fleet-switcher-groups.md). Data layer only — no UI — so this pins the wire
// contract: each method hits the right hub path, method and JSON body. `fetch` is stubbed to
// a 200 so the real `request` path runs (routing + serialization), and we read back the call.

function stubFetch(body: unknown) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }),
  );
}

function lastCall(spy: ReturnType<typeof stubFetch>) {
  const calls = spy.mock.calls;
  const [url, init] = calls[calls.length - 1] as [string, RequestInit];
  return { url: String(url), method: init.method, body: init.body ? JSON.parse(String(init.body)) : undefined };
}

afterEach(() => vi.restoreAllMocks());

describe("fleet group + pin api client", () => {
  it("creates a group with a POST to /api/fleet/groups", async () => {
    const spy = stubFetch({ ok: true, group: { id: "g-1", name: "Writers" } });
    const res = await fleetApi.createFleetGroup("Writers");
    expect(res.group).toEqual({ id: "g-1", name: "Writers" });
    const c = lastCall(spy);
    expect(c.url).toContain("/api/fleet/groups");
    expect(c.method).toBe("POST");
    expect(c.body).toEqual({ name: "Writers" });
  });

  it("renames a group with a PATCH to its id", async () => {
    const spy = stubFetch({ ok: true, group: { id: "g-1", name: "Editors" } });
    await fleetApi.renameFleetGroup("g-1", "Editors");
    const c = lastCall(spy);
    expect(c.url).toContain("/api/fleet/groups/g-1");
    expect(c.method).toBe("PATCH");
    expect(c.body).toEqual({ name: "Editors" });
  });

  it("reorders groups with a PUT to /api/fleet/groups/order", async () => {
    const spy = stubFetch({ ok: true, groups: [] });
    await fleetApi.reorderFleetGroups(["g-2", "g-1"]);
    const c = lastCall(spy);
    expect(c.url).toContain("/api/fleet/groups/order");
    expect(c.method).toBe("PUT");
    expect(c.body).toEqual({ order: ["g-2", "g-1"] });
  });

  it("deletes a group with a DELETE to its id", async () => {
    const spy = stubFetch({ ok: true, id: "g-1", removed: true, ungrouped: 2 });
    const res = await fleetApi.deleteFleetGroup("g-1");
    expect(res.ungrouped).toBe(2);
    const c = lastCall(spy);
    expect(c.url).toContain("/api/fleet/groups/g-1");
    expect(c.method).toBe("DELETE");
  });

  it("sets an agent's group (and clears it with null)", async () => {
    const spy = stubFetch({ ok: true, id: "ava-1", group: "g-1" });
    await fleetApi.setFleetAgentGroup("ava-1", "g-1");
    let c = lastCall(spy);
    expect(c.url).toContain("/api/fleet/agents/ava-1/group");
    expect(c.method).toBe("PUT");
    expect(c.body).toEqual({ group: "g-1" });

    const clearSpy = stubFetch({ ok: true, id: "ava-1", group: null });
    await fleetApi.setFleetAgentGroup("ava-1", null);
    c = lastCall(clearSpy);
    expect(c.body).toEqual({ group: null });
  });

  it("pins and unpins an agent on /api/fleet/agents/{id}/pin", async () => {
    const pinSpy = stubFetch({ ok: true, id: "ava-1", pinned: true, pinned_at: "2026-11-03T00:00:00+00:00" });
    const pinned = await fleetApi.pinFleetAgent("ava-1");
    expect(pinned.pinned).toBe(true);
    let c = lastCall(pinSpy);
    expect(c.url).toContain("/api/fleet/agents/ava-1/pin");
    expect(c.method).toBe("PUT");

    const unpinSpy = stubFetch({ ok: true, id: "ava-1", pinned: false });
    await fleetApi.unpinFleetAgent("ava-1");
    c = lastCall(unpinSpy);
    expect(c.url).toContain("/api/fleet/agents/ava-1/pin");
    expect(c.method).toBe("DELETE");
  });
});
