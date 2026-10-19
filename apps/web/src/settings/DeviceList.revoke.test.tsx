// Removing a device that the server refuses (a network error, a 403, a 500) must say so — a
// Remove button that silently re-enables reads as a dead one (#4350 review). Mounted for real;
// only the toast and the API are stubbed.

import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Toast = { tone?: string; title: string; message?: unknown };
const mocks = vi.hoisted(() => ({
  toasts: [] as Toast[],
  revokeDevice: vi.fn(),
}));
vi.mock("@protolabsai/ui/overlays", () => ({
  useToast: () => (t: Toast) => {
    mocks.toasts.push(t);
    return "t1";
  },
}));
vi.mock("../lib/api", () => ({ api: { revokeDevice: mocks.revokeDevice } }));

import { DeviceList } from "./DeviceList";

import type { PairedDevice } from "../lib/api";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const tablet = {
  id: "d2",
  name: "tablet",
  kind: "device",
  scope: "device",
  created_at: 1,
  last_seen_at: null,
} as unknown as PairedDevice;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
const onChanged = vi.fn(async () => {});

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(h(DeviceList, { devices: [tablet], loading: false, onChanged })));
}

function removeButton(): HTMLButtonElement {
  const btn = host!.querySelector<HTMLButtonElement>('button[aria-label="Remove tablet"]');
  if (!btn) throw new Error("no Remove button");
  return btn;
}

beforeEach(() => {
  window.localStorage.clear();
  mocks.toasts.length = 0;
  mocks.revokeDevice.mockReset();
  onChanged.mockClear();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe("DeviceList ▸ Remove", () => {
  it("a refused revoke shows an error toast with the server's reason and re-enables the button", async () => {
    mocks.revokeDevice.mockRejectedValue(new Error("Only a full-access device can change other devices."));
    await mount();
    await act(async () => removeButton().click());
    expect(mocks.revokeDevice).toHaveBeenCalledWith("d2");
    expect(mocks.toasts).toEqual([
      { tone: "error", title: "Couldn't remove tablet", message: "Only a full-access device can change other devices." },
    ]);
    expect(onChanged).not.toHaveBeenCalled();
    expect(removeButton().disabled).toBe(false);
  });

  it("a non-Error rejection still says something", async () => {
    mocks.revokeDevice.mockRejectedValue("boom");
    await mount();
    await act(async () => removeButton().click());
    expect(mocks.toasts).toEqual([{ tone: "error", title: "Couldn't remove tablet", message: "Try again." }]);
  });

  it("a successful revoke confirms and refreshes", async () => {
    mocks.revokeDevice.mockResolvedValue({ ok: true });
    await mount();
    await act(async () => removeButton().click());
    expect(mocks.toasts).toEqual([{ title: "Removed tablet", message: "Its token no longer works." }]);
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
});
