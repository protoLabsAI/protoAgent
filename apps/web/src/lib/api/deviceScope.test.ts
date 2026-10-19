import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, DEVICE_SCOPE_MESSAGE, deviceScopeMessage, isDeviceScopeError, request } from "./http";
import { runtimeApi } from "./runtime";

// ADR 0121 D4: a paired phone is `device` scope. The server refuses it pairing, plugin
// lifecycle, secrets and credential-plane settings with a 403 `device_scope` — which the
// console must show as a sentence, never as a raw code.

function respond(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })),
  );
}

afterEach(() => vi.unstubAllGlobals());

async function failure(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected the request to fail");
}

const ROUTE_REFUSAL = {
  error: "device_scope",
  detail: { code: "device_scope", reason: DEVICE_SCOPE_MESSAGE, action: "install a plugin" },
};

describe("device-scope refusals", () => {
  it("surface the server's reason through request()", async () => {
    respond(403, ROUTE_REFUSAL);
    const err = await failure(request("/api/plugins/install", { method: "POST", body: {} }));
    expect(err).toBeInstanceOf(ApiError);
    expect(isDeviceScopeError(err)).toBe(true);
    expect(err.message).toBe(DEVICE_SCOPE_MESSAGE);
  });

  it("keep a config refusal's key-naming reason", async () => {
    const reason = "This device can't change network.bind — use a full-access device.";
    respond(403, { ok: false, error: "device_scope", keys: ["network.bind"], detail: { code: "device_scope", reason } });
    const err = await failure(request("/api/settings", { method: "POST", body: {} }));
    expect(err.message).toBe(reason);
  });

  it("never show the bare code even when the reason is missing", async () => {
    respond(403, { detail: { code: "device_scope" } });
    const err = await failure(request("/api/secrets/sync", { method: "POST" }));
    expect(err.message).toBe(DEVICE_SCOPE_MESSAGE);
  });

  it("are not confused with other 403s", async () => {
    respond(403, { detail: "Forbidden: operator credential required" });
    const err = await failure(request("/api/config"));
    expect(isDeviceScopeError(err)).toBe(false);
    expect(err.message).toBe("Forbidden: operator credential required");
  });

  it("read readably from a raw body", () => {
    expect(deviceScopeMessage(ROUTE_REFUSAL)).toBe(DEVICE_SCOPE_MESSAGE);
    expect(deviceScopeMessage({ error: "device_scope" })).toBe(DEVICE_SCOPE_MESSAGE);
    expect(deviceScopeMessage({ error: "invalid or expired pairing code" })).toBeNull();
    expect(deviceScopeMessage(null)).toBeNull();
  });

  it("show readably on the directly-read pairing start", async () => {
    respond(403, ROUTE_REFUSAL);
    const res = await runtimeApi.pairingStart("device");
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toBe(DEVICE_SCOPE_MESSAGE);
  });

  it("show readably on the directly-read tailscale serve", async () => {
    respond(403, ROUTE_REFUSAL);
    const res = await runtimeApi.tailscaleServe(true);
    expect(res.ok).toBe(false);
    expect(res.error).toBe(DEVICE_SCOPE_MESSAGE);
  });

  it("promote/limit sends only the scope", async () => {
    const spy = vi.fn(async () => new Response(JSON.stringify({ ok: true, device: {} }), { status: 200 }));
    vi.stubGlobal("fetch", spy);
    await runtimeApi.setDeviceScope("abc", "full");
    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ scope: "full" });
  });
});
