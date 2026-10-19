import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { markDeviceMoved, storeDeviceCredential } from "./credentials";
import { rotateDeviceTokenIfDue } from "./deviceRotation";
import { __resetStorageSeamForTests } from "./storage";

// ADR 0121 D1 — the two ways a phone learns its move away from this origin completed (the
// server says `handed_off`, or the handed-off token is refused after its grace) share ONE
// completion step: forget the token only if it is still the stored one, and ask for auth only
// when no operator token is left.

const auth = vi.hoisted(() => ({ notifyAuthRequired: vi.fn() }));
vi.mock("./auth", () => auth);

const DEV = "protoagent.deviceToken";
const OP = "protoagent.authToken";

type Path = "handed_off" | "refused_after_grace";
const PATHS: Path[] = ["handed_off", "refused_after_grace"];

/** Answer /api/devices/self for `path`; `during` runs while the request is in flight. */
function serveSelf(path: Path, during: () => void = () => {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      during();
      if (path === "refused_after_grace") return new Response("{}", { status: 401 });
      const device = { id: "d1", name: "phone", rotate_due: true, handed_off: true, created_at: 1, last_seen_at: null };
      return new Response(JSON.stringify({ device }), { status: 200 });
    }),
  );
}

describe.each(PATHS)("a completed move (%s)", (path) => {
  beforeEach(() => {
    window.localStorage.clear();
    auth.notifyAuthRequired.mockClear();
    storeDeviceCredential("tok", "d1");
    markDeviceMoved("https://box.tail1234.ts.net");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    __resetStorageSeamForTests();
  });

  it("forgets the token and asks for auth when nothing else signs this console in", async () => {
    serveSelf(path);
    expect(await rotateDeviceTokenIfDue()).toBe("moved");
    expect(window.localStorage.getItem(DEV)).toBeNull();
    expect(auth.notifyAuthRequired).toHaveBeenCalledTimes(1);
  });

  it("forgets the token but stays signed in with an operator token", async () => {
    window.localStorage.setItem(OP, "op");
    serveSelf(path);
    expect(await rotateDeviceTokenIfDue()).toBe("moved");
    expect(window.localStorage.getItem(DEV)).toBeNull();
    expect(auth.notifyAuthRequired).not.toHaveBeenCalled();
  });

  it("leaves a token another tab stored meanwhile alone", async () => {
    serveSelf(path, () => window.localStorage.setItem(DEV, "re-paired"));
    expect(await rotateDeviceTokenIfDue()).toBe("moved");
    expect(window.localStorage.getItem(DEV)).toBe("re-paired");
    expect(auth.notifyAuthRequired).not.toHaveBeenCalled();
  });
});
