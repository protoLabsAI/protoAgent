import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { enrollPush, PushError, pushStatus, pushState, renewPush, startPush, unenrollPush, removeLocalPushSubscription } from "./push";
import { rotateDeviceTokenIfDue } from "./deviceRotation";
import { claimPairingCode } from "./pairing";
import { requestHandoff } from "./deviceHandoff";

let subscription: ReturnType<typeof makeSubscription> | null;
const unsubscribe = vi.fn(async () => { subscription = null; return true; });
function makeSubscription(key = new Uint8Array([1, 2, 3]).buffer) {
  return { options: { applicationServerKey: key }, expirationTime: null, unsubscribe,
    toJSON: () => ({ endpoint: "https://fcm.googleapis.com/sub", keys: { p256dh: "p", auth: "a" } }) };
}
const subscribe = vi.fn(async ({ applicationServerKey }) => {
  subscription = makeSubscription(applicationServerKey.buffer);
  return subscription;
});
const registration = { active: {}, pushManager: { getSubscription: vi.fn(async () => subscription), subscribe } };
const register = vi.fn(async () => registration);
const permission = { permission: "granted" };
const requestPermission = vi.fn(async () => { permission.permission = "granted"; return "granted"; });
let status: { public_key: string; version: number; binding: { vapid_version: number } | null; renew: string | null };
const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => new Response(JSON.stringify(init?.method === "GET" ? status : { ok: true })));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  window.history.replaceState(null, "", "/app/agent/member/");
  localStorage.setItem("protoagent.deviceToken", "own-token");
  localStorage.setItem("protoagent.authToken", "operator-token");
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("PushManager", class {});
  permission.permission = "granted";
  vi.stubGlobal("Notification", Object.assign(permission, { requestPermission }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("navigator", { serviceWorker: { register, getRegistration: async () => registration } });
  subscription = null;
  status = { public_key: "AQID", version: 2, binding: null, renew: null };
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each(["vapid_key", "handoff", "idle", null] as const)("reads the full self binding with renewal reason %s", async (renew) => {
  const binding = { vapid_version: 2, generation: 3, created_at: 100, renewed_at: 200 };
  status = { ...status, binding, renew };
  expect(await pushStatus()).toEqual({ public_key: "AQID", version: 2, binding, renew });
  expect(fetchMock.mock.calls[0][0]).toBe("/api/devices/self/push");
  const init = fetchMock.mock.calls[0][1];
  expect(init).toMatchObject({ method: "GET", cache: "no-store" });
  expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer own-token");
  status.binding = null;
  expect((await pushStatus()).binding).toBeNull();
});

it.each([
  [403, '{"error":"not_a_device"}', "not_a_device"],
  [503, '{"error":"push_keyring_unavailable"}', "push_keyring_unavailable"],
  [502, "Bad gateway", null],
  [500, '{"error":123}', null],
  [500, "null", null],
  [500, "{}", null],
] as const)("preserves HTTP %s and code from %s", async (httpStatus, body, code) => {
  fetchMock.mockResolvedValueOnce(new Response(body, { status: httpStatus }));
  const result = pushStatus();
  await expect(result).rejects.toBeInstanceOf(PushError);
  await expect(result).rejects.toMatchObject({ status: httpStatus, code, message: `Push enrollment: HTTP ${httpStatus}` });
  // Failed reads must not poison the serialization queue.
  expect((await pushStatus()).binding).toBeNull();
});

it.each([
  [400, "invalid_endpoint"], [400, "invalid_keys"], [409, "stale_vapid_version"],
  [409, "endpoint_in_use"], [409, "not_enrollable"],
] as const)("enrollment surfaces PUT refusal %s %s", async (httpStatus, code) => {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(status)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ error: code }), { status: httpStatus }));
  const result = enrollPush();
  await expect(result).rejects.toBeInstanceOf(PushError);
  await expect(result).rejects.toMatchObject({ status: httpStatus, code });
  expect(fetchMock.mock.calls[1][1]?.method).toBe("PUT");
});

it("queues status behind subscription changes and reads the current credential", async () => {
  let finish!: (response: Response) => void;
  fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve; }));
  const removal = unenrollPush();
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  const read = pushStatus();
  expect(fetchMock).toHaveBeenCalledOnce();
  localStorage.setItem("protoagent.deviceToken", "new-token");
  finish(new Response(JSON.stringify({ ok: true })));
  await removal;
  await read;
  expect(fetchMock.mock.calls.map(([, init]) => init?.method)).toEqual(["DELETE", "GET"]);
  expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("Authorization")).toBe("Bearer new-token");
});

it.each(["insecure", "missing PushManager"])("does nothing when %s", async (condition) => {
  if (condition === "insecure") vi.stubGlobal("isSecureContext", false);
  else vi.stubGlobal("PushManager", undefined);
  expect(await pushState()).toBe("unsupported");
  await startPush();
  expect(register).not.toHaveBeenCalled();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("startup registers without requesting permission", async () => {
  permission.permission = "default";
  await startPush();
  expect(register).toHaveBeenCalledWith("/app/sw.js", { scope: "/app/" });
  expect(requestPermission).not.toHaveBeenCalled();
  expect(subscribe).not.toHaveBeenCalled();
});

it.each(["key rotation", "cleared binding"])("startup renews a %s", async (reason) => {
  subscription = makeSubscription();
  status.binding = reason === "key rotation" ? { vapid_version: 1 } : null;
  await startPush();
  expect(unsubscribe).toHaveBeenCalledOnce();
  expect(subscribe).toHaveBeenCalledOnce();
  expect(requestPermission).not.toHaveBeenCalled();
  const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(put[0]).toBe("/api/devices/self/push");
  expect(new Headers(put[1]?.headers).get("Authorization")).toBe("Bearer own-token");
  expect(JSON.parse(String(put[1]?.body))).toMatchObject({ vapid_version: 2, keys: { auth: "a" } });
});

it("denial stores nothing and a gesture grant PUTs one own binding", async () => {
  permission.permission = "denied";
  expect(await enrollPush()).toBe("denied");
  expect(fetchMock).not.toHaveBeenCalled();
  expect(localStorage.getItem("protoagent.pushDisabled")).toBeNull();
  permission.permission = "default";
  const enrollment = enrollPush();
  expect(requestPermission).toHaveBeenCalledOnce();
  expect(fetchMock).not.toHaveBeenCalled();
  expect(await enrollment).toBe("enrolled");
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
  status.binding = { vapid_version: 2 };
  expect(await pushState()).toBe("enrolled");
});

it("failed reads preserve the subscription and are retried on the next visit", async () => {
  subscription = makeSubscription();
  fetchMock.mockRejectedValueOnce(new TypeError("offline"));
  await expect(renewPush()).rejects.toThrow("offline");
  expect(unsubscribe).not.toHaveBeenCalled();
  expect(await renewPush()).toBe("enrolled");
});

it("unenrollment deletes with the device token and survives a restart without prompting", async () => {
  subscription = makeSubscription();
  await unenrollPush();
  expect(fetchMock.mock.calls[0][1]?.method).toBe("DELETE");
  expect(unsubscribe).toHaveBeenCalledOnce();
  await startPush();
  expect(subscribe).not.toHaveBeenCalled();
});

it("rotation re-enrolls with the newly stored token", async () => {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ device: { rotate_due: true } })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ token: "rotated-token" })));
  expect(await rotateDeviceTokenIfDue()).toBe("rotated");
  const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(new Headers(put[1]?.headers).get("Authorization")).toBe("Bearer rotated-token");
});

it("keeps an offered handoff subscribed; confirmed handoff removes only the old browser subscription", async () => {
  subscription = makeSubscription();
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ url: "https://new.example/app/#pair=code" })));
  expect((await requestHandoff("https://new.example")).ok).toBe(true);
  expect(unsubscribe).not.toHaveBeenCalled();
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ device: { handed_off: true } })));
  expect(await rotateDeviceTokenIfDue()).toBe("moved");
  expect(unsubscribe).toHaveBeenCalledOnce();
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
});

it("a successful handoff claim enrolls the new home using its own credential", async () => {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
    token: "new-home-token", device: { id: "same-device" }, moved: true,
  })));
  expect(await claimPairingCode("CODE")).toEqual({ ok: true });
  await vi.waitFor(() => {
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(put).toBeDefined();
    expect(new Headers(put![1]?.headers).get("Authorization")).toBe("Bearer new-home-token");
  });
  expect(requestPermission).not.toHaveBeenCalled();
});

it("a queued renewal respects an intervening opt-out", async () => {
  const renewal = renewPush();
  const removal = unenrollPush();
  expect(await renewal).toBe("not enrolled");
  await removal;
  expect(subscribe).not.toHaveBeenCalled();
});

it("old-home cleanup cannot remove a subscription after a concurrent re-pair", async () => {
  subscription = makeSubscription();
  await removeLocalPushSubscription("retired-token");
  expect(unsubscribe).not.toHaveBeenCalled();
});
