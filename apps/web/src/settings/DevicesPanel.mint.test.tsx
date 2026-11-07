// "New access token" (#4683): Settings ▸ Devices mints a NAMED device-scope token with no
// phone to pair, shown ONCE. The button is for full-scope callers only — a device-scope caller
// (whose server would 403 the mint) must not see a dead button, mirroring how DeviceList hides
// Promote/Limit. The panel is mounted for real; only the toast, fetch and the clipboard are
// stubbed.

import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Toast = { tone?: string; title: string; message?: unknown };
const mocks = vi.hoisted(() => ({
  toasts: [] as Toast[],
  devices: vi.fn(),
  mintDeviceToken: vi.fn(),
  copyText: vi.fn(),
}));

vi.mock("@protolabsai/ui/overlays", () => ({
  useToast: () => (t: Toast) => {
    mocks.toasts.push(t);
    return "t1";
  },
}));
vi.mock("../lib/clipboard", () => ({ copyText: mocks.copyText }));

import { DevicesPanel } from "./DevicesPanel";

import type { PairedDevice } from "../lib/api";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const fullCaller = { scope: "full", device_id: null as string | null };
const deviceCaller = { scope: "device", device_id: "self" as string | null };

function listResult(devices: PairedDevice[] = [], caller = fullCaller) {
  return { devices, caller, public_console_url: "" };
}

// Deliberately obvious placeholder: this UI does not require credential-shaped fixtures.
const sampleToken = "test-token";

const newRow = {
  id: "d-new",
  name: "CI uploader",
  kind: "device",
  scope: "device",
  created_at: 10,
  last_seen_at: null,
} as unknown as PairedDevice;

let root: Root | null = null;
let host: HTMLDivElement | null = null;

/** Let refresh()/mintToken()'s awaited promise chains (api.devices → setState) settle. Each
 *  iteration is its own `act`, so every state flush is covered and no update escapes act(). */
async function flush(times = 4) {
  for (let i = 0; i < times; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(h(DevicesPanel));
  });
  await flush();
}

function buttonWith(text: string): HTMLButtonElement | null {
  return (
    Array.from(host!.querySelectorAll<HTMLButtonElement>("button")).find((b) =>
      (b.textContent ?? "").includes(text),
    ) ?? null
  );
}

function buttonExactly(text: string): HTMLButtonElement | null {
  return (
    Array.from(host!.querySelectorAll<HTMLButtonElement>("button")).find((b) => (b.textContent ?? "").trim() === text) ??
    null
  );
}

function nameInput(): HTMLInputElement {
  const el = host!.querySelector<HTMLInputElement>('input[aria-label="Name for the new access token"]');
  if (!el) throw new Error("no name input");
  return el;
}

function setValue(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

async function submitForm() {
  const form = host!.querySelector<HTMLFormElement>("form.devices-rename");
  if (!form) throw new Error("no mint form");
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
}

function tokenCode(): Element | null {
  return host!.querySelector('[data-testid="minted-device-token"]');
}

async function openMint() {
  await act(async () => buttonWith("New access token")!.click());
}

beforeEach(() => {
  window.localStorage.clear();
  mocks.toasts.length = 0;
  mocks.devices.mockReset();
  mocks.mintDeviceToken.mockReset();
  mocks.copyText.mockReset();
  mocks.copyText.mockResolvedValue(true);
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url !== "/api/devices") throw new Error(`Unexpected request: ${url}`);
    if (init?.method === "POST") {
      return mocks.mintDeviceToken(JSON.parse(init.body as string).name);
    }
    return Response.json(await mocks.devices());
  }));
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  vi.unstubAllGlobals();
  root = null;
  host = null;
});

describe("DevicesPanel ▸ New access token", () => {
  it("shows the button for a full-scope caller and hides it for a device-scope one", async () => {
    mocks.devices.mockResolvedValue(listResult([], fullCaller));
    await mount();
    expect(buttonWith("New access token")).not.toBeNull();

    await act(async () => root?.unmount());
    host?.remove();
    mocks.devices.mockResolvedValue(listResult([], deviceCaller));
    await mount();
    expect(buttonWith("New access token")).toBeNull();
  });

  it("mints a named token, shows it once with a working Copy, and refreshes the list", async () => {
    mocks.devices.mockResolvedValue(listResult([], fullCaller));
    mocks.mintDeviceToken.mockImplementation(() => Response.json({ ok: true, device: newRow, token: sampleToken }));
    await mount();

    await openMint();
    await act(async () => setValue(nameInput(), "CI uploader"));
    // The post-mint refresh returns the new (Limited) row.
    mocks.devices.mockResolvedValue(listResult([newRow], fullCaller));
    await submitForm();
    await flush();

    expect(mocks.mintDeviceToken).toHaveBeenCalledWith("CI uploader");
    expect(fetch).toHaveBeenCalledWith("/api/devices", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ name: "CI uploader" }),
    }));
    expect(mocks.devices).toHaveBeenCalledTimes(2);
    expect(tokenCode()?.textContent).toBe(sampleToken);
    // The new device row is now in the list below.
    expect(host!.querySelector('[data-testid="device-row-d-new"]')).not.toBeNull();

    await act(async () => buttonWith("Copy")!.click());
    await flush();
    expect(mocks.copyText).toHaveBeenCalledWith(sampleToken);
    expect(mocks.toasts).toContainEqual({ title: "Token copied", message: "Keep it somewhere safe." });
  });

  it("can't submit an empty name", async () => {
    mocks.devices.mockResolvedValue(listResult([], fullCaller));
    await mount();
    await openMint();

    expect(buttonWith("Create token")!.disabled).toBe(true);
    await submitForm();
    await flush();
    expect(mocks.mintDeviceToken).not.toHaveBeenCalled();
    expect(tokenCode()).toBeNull();
  });

  it.each([
    [400, "A device name is required."],
    [403, { code: "device_scope", reason: "Only a full-access device can mint an access token." }],
  ])("surfaces the server's detail on a %s refusal and shows no token", async (status, detail) => {
    mocks.devices.mockResolvedValue(listResult([], fullCaller));
    mocks.mintDeviceToken.mockImplementation(() => Response.json({ detail }, { status }));
    await mount();

    await openMint();
    await act(async () => setValue(nameInput(), "CI uploader"));
    await submitForm();
    await flush();

    expect(mocks.toasts).toContainEqual({
      tone: "error",
      title: "Couldn't create an access token",
      message: typeof detail === "string" ? detail : detail.reason,
    });
    expect(tokenCode()).toBeNull();
  });

  it("dismissing the token block removes it from the DOM", async () => {
    mocks.devices.mockResolvedValue(listResult([], fullCaller));
    mocks.mintDeviceToken.mockImplementation(() => Response.json({ ok: true, device: newRow, token: sampleToken }));
    await mount();

    await openMint();
    await act(async () => setValue(nameInput(), "CI uploader"));
    mocks.devices.mockResolvedValue(listResult([newRow], fullCaller));
    await submitForm();
    await flush();
    expect(tokenCode()).not.toBeNull();

    await act(async () => buttonExactly("Dismiss")!.click());
    expect(tokenCode()).toBeNull();
    expect(host!.textContent).not.toContain(sampleToken);
    expect(window.localStorage.length).toBe(0);
  });
});
