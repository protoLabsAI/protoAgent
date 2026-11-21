// Settings ▸ Devices ▸ "Notification key" (ADR 0135): the card is mounted for real with the
// DS button; only the two key routes on `api` are stubbed.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, type PushKeys, type PushKeyVersion } from "../lib/api";
import { KEY_COPY, NotificationKeyCard, windowDays } from "./NotificationKeyCard";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Local noon, so "18 Dec" holds in whatever time zone the suite runs.
const RETIRES = new Date(new Date().getFullYear() + 1, 11, 18, 12).getTime() / 1000;
const key = (version: number, over: Partial<PushKeyVersion> = {}): PushKeyVersion => ({
  version,
  created_at: 1,
  active: false,
  retire_after: null,
  ...over,
});
const keyring = (oldCount: number): PushKeys => ({
  keys: [key(1, { retire_after: RETIRES }), key(2, { active: true })],
  rotation_window_seconds: 2592000,
  bindings_by_version: { "1": oldCount, "2": 3 },
});

let root: Root;
let node: HTMLDivElement;

beforeEach(() => {
  node = document.createElement("div");
  document.body.append(node);
  root = createRoot(node);
});
afterEach(async () => {
  await act(async () => root.unmount());
  node.remove();
  vi.restoreAllMocks();
});

async function render(fullAccess = true) {
  await act(async () => root.render(<NotificationKeyCard fullAccess={fullAccess} />));
  await act(async () => {});
}
const card = () => node.querySelector<HTMLElement>('[data-testid="notification-key-card"]');
const button = (name: string) =>
  Array.from(node.querySelectorAll("button")).find((b) => b.textContent?.trim() === name) ?? null;
const group = () => node.querySelector<HTMLElement>('[role="group"][aria-label="Confirm key rotation"]');
async function click(name: string) {
  await act(async () => button(name)!.click());
}

describe("NotificationKeyCard", () => {
  it("is absent for a device-scope caller and asks the server nothing", async () => {
    const get = vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(2));
    await render(false);
    expect(card()).toBeNull();
    expect(node.textContent).toBe("");
    expect(get).not.toHaveBeenCalled();
  });

  it("renders no DOM when no key exists", async () => {
    const get = vi.spyOn(api, "pushKeys").mockResolvedValue({
      keys: [],
      rotation_window_seconds: 2592000,
      bindings_by_version: {},
    });
    await render();
    expect(get).toHaveBeenCalledTimes(1);
    expect(card()).toBeNull();
    expect(node.innerHTML).toBe("");
  });

  it("renders no DOM while the first read is still out", async () => {
    vi.spyOn(api, "pushKeys").mockReturnValue(new Promise<PushKeys>(() => {}));
    await render();
    expect(node.innerHTML).toBe("");
  });

  it("renders no DOM for a version that is neither active nor retiring", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue({
      keys: [key(1)],
      rotation_window_seconds: 2592000,
      bindings_by_version: { "1": 2 },
    });
    await render();
    expect(node.innerHTML).toBe("");
  });

  it("renders the card for a retiring key with no active one, and offers no rotation", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue({
      keys: [key(1, { retire_after: RETIRES })],
      rotation_window_seconds: 2592000,
      bindings_by_version: { "1": 2 },
    });
    await render();
    expect(card()!.textContent).toContain("Version 1 retires on 18 Dec. 2 devices still use it.");
    expect(card()!.textContent).not.toContain("No notification key yet");
    expect(card()!.textContent).not.toContain("active");
    expect(button("Rotate key…")).toBeNull();
  });

  it("shows the active version, the retiring version and how many devices still use it", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(2));
    await render();
    expect(card()!.textContent).toContain("Key version 2 · active");
    expect(card()!.textContent).toContain("Version 1 retires on 18 Dec. 2 devices still use it.");
    expect(button("Rotate key…")).not.toBeNull();
  });

  it("says one device in the singular", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(1));
    await render();
    expect(card()!.textContent).toContain("Version 1 retires on 18 Dec. 1 device still uses it.");
  });

  it("hides the count sentence when no device uses the retiring version", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(0));
    await render();
    const line = node.querySelector('[data-testid="notification-key-retiring-1"]')!;
    expect(line.textContent).toBe("Version 1 retires on 18 Dec.");
    expect(card()!.textContent).not.toContain("still use");
  });

  it("never shows key material", async () => {
    const withKey = keyring(2);
    (withKey.keys[1] as PushKeyVersion & { public_key: string }).public_key = "BPUBLICKEYMATERIAL";
    vi.spyOn(api, "pushKeys").mockResolvedValue(withKey);
    await render();
    expect(node.innerHTML).not.toContain("BPUBLICKEYMATERIAL");
  });

  it("takes the confirm's day count from rotation_window_seconds and focuses Cancel", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(2));
    await render();
    expect(group()).toBeNull();
    await click("Rotate key…");
    expect(group()!.textContent).toContain(
      "Every device must renew within 30 days. Devices that don't renew stop getting alerts.",
    );
    expect(document.activeElement).toBe(button("Cancel"));
    expect(button("Rotate key")).not.toBeNull();
    expect(windowDays(86400)).toBe("1 day");
    expect(windowDays(7 * 86400 + 3600)).toBe("7 days");
  });

  it("Escape cancels without propagating and refocuses Rotate key…", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(2));
    const post = vi.spyOn(api, "rotatePushKey");
    const outer = vi.fn();
    document.addEventListener("keydown", outer);
    try {
      await render();
      await click("Rotate key…");
      await act(async () => {
        button("Cancel")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
      });
      expect(group()).toBeNull();
      expect(outer).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(button("Rotate key…"));
      expect(post).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("keydown", outer);
    }
  });

  it("Cancel closes the confirm and refocuses Rotate key…", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(2));
    await render();
    await click("Rotate key…");
    await click("Cancel");
    expect(group()).toBeNull();
    expect(document.activeElement).toBe(button("Rotate key…"));
  });

  it("Rotate key posts, then reloads the key list", async () => {
    const order: string[] = [];
    const rotated: PushKeys = {
      keys: [key(2, { retire_after: RETIRES }), key(3, { active: true })],
      rotation_window_seconds: 2592000,
      bindings_by_version: { "2": 3, "3": 0 },
    };
    const get = vi.spyOn(api, "pushKeys").mockImplementation(async () => {
      order.push("get");
      return order.includes("post") ? rotated : keyring(0);
    });
    const post = vi.spyOn(api, "rotatePushKey").mockImplementation(async () => {
      order.push("post");
      return key(3, { active: true });
    });
    await render();
    await click("Rotate key…");
    await click("Rotate key");
    await act(async () => {});
    expect(post).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledTimes(2);
    expect(order).toEqual(["get", "post", "get"]);
    expect(card()!.textContent).toContain("Key version 3 · active");
    expect(card()!.textContent).toContain("Version 2 retires on 18 Dec. 3 devices still use it.");
    expect(group()).toBeNull();
    expect(node.querySelector('[role="status"]')!.textContent).toBe("Key version 3 is now active.");
    expect(document.activeElement).toBe(button("Rotate key…"));
  });

  it("a 403 on rotate removes the card: the caller lacks full access", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(2));
    vi.spyOn(api, "rotatePushKey").mockRejectedValue(new ApiError(403, "forbidden", "device_scope"));
    await render();
    await click("Rotate key…");
    await click("Rotate key");
    await act(async () => {});
    expect(card()).toBeNull();
    expect(node.innerHTML).toBe("");
  });

  it("a 403 on the read renders no DOM: the caller lacks full access", async () => {
    vi.spyOn(api, "pushKeys").mockRejectedValue(new ApiError(403, "forbidden"));
    await render();
    expect(card()).toBeNull();
    expect(node.innerHTML).toBe("");
  });

  it("another rotate failure keeps the confirm open and says so", async () => {
    vi.spyOn(api, "pushKeys").mockResolvedValue(keyring(2));
    vi.spyOn(api, "rotatePushKey").mockRejectedValue(new ApiError(503, "push_keyring_unavailable"));
    await render();
    await click("Rotate key…");
    await click("Rotate key");
    await act(async () => {});
    expect(group()!.querySelector('[role="alert"]')!.textContent).toBe(KEY_COPY.rotateFailed);
    expect(button("Rotate key")!.disabled).toBe(false);
  });

  it("a failed read offers Try again, and the card goes away when the retry finds no key", async () => {
    const get = vi.spyOn(api, "pushKeys").mockRejectedValueOnce(new Error("offline"));
    get.mockResolvedValue({ keys: [], rotation_window_seconds: 2592000, bindings_by_version: {} });
    await render();
    expect(card()!.textContent).toContain(KEY_COPY.loadFailed);
    await click("Try again");
    await act(async () => {});
    expect(card()).toBeNull();
    expect(node.innerHTML).toBe("");
  });
});
