import { afterEach, expect, it, vi } from "vitest";
import { request } from "./http";

afterEach(() => {
  window.dispatchEvent(new Event("pageshow"));
  vi.unstubAllGlobals();
});

it("stops polling reads during pagehide and resumes after a bfcache restore", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ turns: [] }) });
  vi.stubGlobal("fetch", fetch);
  const path = "/api/chat/sessions/session/local-turns";
  await expect(request(path)).resolves.toEqual({ turns: [] });
  window.dispatchEvent(new Event("pagehide"));
  await expect(request(path)).rejects.toMatchObject({ name: "AbortError" });
  await expect(request("/api/fleet", { method: "get" })).rejects.toMatchObject({ name: "AbortError" });
  expect(fetch).toHaveBeenCalledTimes(1);
  window.dispatchEvent(new Event("pageshow"));
  await expect(request(path)).resolves.toEqual({ turns: [] });
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("preserves reads after a cancelled unload and writes during pagehide", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) });
  vi.stubGlobal("fetch", fetch);
  const unload = new Event("beforeunload", { cancelable: true });
  unload.preventDefault();
  window.dispatchEvent(unload);
  await expect(request("/api/fleet")).rejects.toMatchObject({ name: "AbortError" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  await expect(request("/api/fleet")).resolves.toEqual({ ok: true });
  window.dispatchEvent(new Event("pagehide"));
  await expect(request("/api/feedback", { method: "POST", body: { feedback: "saved" }, keepalive: true }))
    .resolves.toEqual({ ok: true });
  expect(fetch).toHaveBeenCalledTimes(2);
});

it.each(["beforeunload", "pagehide"])("cancels an in-flight read on %s and preserves caller cancellation", async (event) => {
  vi.stubGlobal("fetch", vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
    const abort = () => reject(new DOMException("Aborted", "AbortError"));
    if (init.signal?.aborted) abort();
    else init.signal?.addEventListener("abort", abort, { once: true });
  })));
  const pending = request("/api/fleet");
  window.dispatchEvent(new Event(event));
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  window.dispatchEvent(new Event("pageshow"));
  const caller = new AbortController();
  const next = request("/api/fleet", { signal: caller.signal });
  caller.abort();
  await expect(next).rejects.toMatchObject({ name: "AbortError" });
  await expect(request("/api/fleet", { signal: caller.signal })).rejects.toMatchObject({ name: "AbortError" });
});
