vi.mock("./sessionHydration", () => ({ hydrateDurableChatSessions: vi.fn(async () => {}) }));
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import type { DirectiveRow } from "../lib/api/chat";
import { hydrateDurableChatSessions } from "./sessionHydration";
import { useUI } from "../state/uiStore";

const listeners = new Set<(data: Record<string, unknown>) => void>();
const connections = new Set<(connected: boolean) => void>();
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    expect(topic).toBe("directive.changed");
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  onConnectionChange: (fn: (connected: boolean) => void) => {
    connections.add(fn);
    fn(true);
    return () => connections.delete(fn);
  },
}));

const { directivesStoreForTest } = await import("./useDirectives");
const row = (id: string, patch: Partial<DirectiveRow> = {}): DirectiveRow => ({
  id, session_id: "s1", quote: "Wait for approval", conditions: "", scope: "",
  status: "in_force", created_at: "2026-11-07T17:14:02+00:00",
  closed_by: null, closed_at: null, close_reason: null, ...patch,
});
const list = (directives: DirectiveRow[] = []) => ({
  directives, in_force: directives.filter((d) => d.status === "in_force").length,
});
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
let sequence = 0;
let sessionId: string;
let cleanup: (() => void)[];
beforeEach(() => {
  vi.mocked(hydrateDurableChatSessions).mockClear();
  sessionId = `session-${++sequence}`;
  cleanup = [];
});
afterEach(() => {
  cleanup.forEach((off) => off());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
function mount() {
  const store = directivesStoreForTest(sessionId);
  cleanup.push(store.subscribe(() => {}));
  return store;
}

describe("directive ledger shared store", () => {
  it("keeps active API order and sorts history by newest closure", async () => {
    const old = row("old", { status: "fulfilled", closed_at: "2026-11-07T18:00:00+00:00" });
    const recent = row("recent", { status: "revoked", closed_at: "2026-11-08T09:00:00+00:00" });
    vi.spyOn(api, "listDirectives").mockResolvedValue(list([row("a"), old, recent, row("b")]));
    const store = mount();
    expect(store.getSnapshot().state).toBe("loading");
    await settle();
    expect(store.getSnapshot().active.map((d) => d.id)).toEqual(["a", "b"]);
    expect(store.getSnapshot().history).toEqual([recent, old]);
    expect(store.getSnapshot().state).toBe("ready");
    expect(store.getSnapshot().loadedAt).toEqual(expect.any(Number));
  });

  it("shares initial fetches and subscriptions; filters events and refreshes on reconnect", async () => {
    const fetch = vi.spyOn(api, "listDirectives").mockResolvedValue(list());
    const store = mount();
    expect(mount()).toBe(store);
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(listeners.size).toBe(1);
    listeners.forEach((fn) => fn({ session_id: "elsewhere" }));
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
    listeners.forEach((fn) => fn({ session_id: sessionId }));
    await settle();
    expect(fetch).toHaveBeenCalledTimes(2);
    connections.forEach((fn) => fn(false));
    expect(fetch).toHaveBeenCalledTimes(2);
    connections.forEach((fn) => fn(true));
    await settle();
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(fetch).toHaveBeenLastCalledWith(sessionId);
    cleanup.forEach((off) => off());
    expect(listeners.size).toBe(0);
    expect(connections.size).toBe(0);
  });

  it("does not lose invalidations arriving during an in-flight fetch", async () => {
    let resolve!: (data: ReturnType<typeof list>) => void;
    const fetch = vi.spyOn(api, "listDirectives")
      .mockImplementationOnce(() => new Promise((r) => { resolve = r; }))
      .mockResolvedValue(list([row("new")]));
    const store = mount();
    listeners.forEach((fn) => fn({ session_id: sessionId }));
    listeners.forEach((fn) => fn({ session_id: sessionId }));
    expect(fetch).toHaveBeenCalledTimes(1);
    resolve(list());
    await settle();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot().active[0].id).toBe("new");
  });

  it("stays failed until a success, then preserves rows and loadedAt on failure", async () => {
    const fetch = vi.spyOn(api, "listDirectives").mockRejectedValue(new Error("offline"));
    const store = mount();
    await settle();
    expect(store.getSnapshot()).toMatchObject({ state: "failed", loadedAt: null });
    await store.refresh();
    expect(store.getSnapshot().state).toBe("failed");
    fetch.mockResolvedValue(list([row("good")]));
    await store.refresh();
    const good = store.getSnapshot();
    fetch.mockRejectedValue(new Error("offline again"));
    await store.refresh();
    expect(store.getSnapshot()).toEqual({ ...good, state: "stale" });
    fetch.mockResolvedValue(list());
    await store.refresh();
    expect(store.getSnapshot().state).toBe("ready");
  });

  it.each([200, 409, 404, 500])("handles revoke %i", async (status) => {
    const fetch = vi.spyOn(api, "listDirectives").mockResolvedValue(list([row("a")]));
    const store = mount();
    await settle();
    const closed = row("a", {
      status: "fulfilled", closed_by: "agent", closed_at: "2026-11-08T09:00:00+00:00",
      close_reason: "Completed",
    });
    fetch.mockResolvedValue(list(status === 404 ? [] : [closed]));
    const error = new ApiError(status, "detail");
    const revoke = vi.spyOn(api, "revokeDirective");
    if (status === 200) revoke.mockResolvedValue({ directive: closed });
    else revoke.mockRejectedValue(error);
    const result = await store.revoke("a", "keep my reason");
    expect(revoke).toHaveBeenCalledWith(sessionId, "a", "keep my reason");
    expect(result).toEqual(status === 200 ? { ok: true }
      : status === 409 ? { conflict: closed }
      : status === 404 ? { gone: true } : { error });
    expect(fetch).toHaveBeenCalledTimes(status === 500 ? 1 : 2);
    expect(hydrateDurableChatSessions).toHaveBeenCalledTimes(status === 200 ? 1 : 0);
  });

  it("does not present an old row as a fresh conflict when refetch fails", async () => {
    const fetch = vi.spyOn(api, "listDirectives").mockResolvedValue(list([row("a")]));
    const store = mount();
    await settle();
    fetch.mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "revokeDirective").mockRejectedValue(new ApiError(409, "closed"));
    expect(await store.revoke("a")).toEqual({ conflict: null });
    expect(store.getSnapshot().state).toBe("stale");
  });
});

describe("directive HTTP helpers", () => {
  it("reads the typed list and encodes path segments and the reason query", async () => {
    const data = list([row("a")]);
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(data)));
    vi.stubGlobal("fetch", fetch);
    expect(await api.listDirectives("chat:one")).toEqual(data);
    expect(String(fetch.mock.calls[0][0])).toContain("/api/chat/sessions/chat%3Aone/directives");
    fetch.mockResolvedValue(new Response(JSON.stringify({ directive: row("a") })));
    const reason = " hold & wait? # café\n";
    await api.revokeDirective("chat:one", "dir:a", reason);
    expect(String(fetch.mock.calls[1][0])).toContain(
      `/api/chat/sessions/chat%3Aone/directives/dir%3Aa?reason=${encodeURIComponent(reason)}`,
    );
    expect(fetch.mock.calls[1][1]).toMatchObject({ method: "DELETE" });
  });

  it.each([undefined, "", " \n\t "])("omits a blank reason (%j)", async (reason) => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ directive: row("a") })));
    vi.stubGlobal("fetch", fetch);
    await api.revokeDirective("s1", "a", reason);
    expect(String(fetch.mock.calls[0][0])).toMatch(/\/directives\/a$/);
  });

  it.each([404, 409])("preserves ApiError status %i", async (status) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "closed or gone" }), { status }),
    ));
    await expect(api.revokeDirective("s1", "a")).rejects.toMatchObject({
      name: "ApiError", status, message: "closed or gone",
    });
  });
});

it("openDirectives bumps the counter across sessions and excludes it from persistence", () => {
  useUI.setState({ directivesRequest: null });
  useUI.getState().openDirectives("s1");
  expect(useUI.getState().directivesRequest).toEqual({ sessionId: "s1", n: 1 });
  useUI.getState().openDirectives("s1");
  expect(useUI.getState().directivesRequest).toEqual({ sessionId: "s1", n: 2 });
  useUI.getState().openDirectives("s2");
  expect(useUI.getState().directivesRequest).toEqual({ sessionId: "s2", n: 3 });
  const persisted = useUI.persist.getOptions().partialize!(useUI.getState());
  expect(persisted).not.toHaveProperty("directivesRequest");
  expect(persisted).toHaveProperty("showChatUsage");
  useUI.setState({ directivesRequest: null });
});
