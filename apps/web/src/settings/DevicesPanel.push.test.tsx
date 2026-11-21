// Settings ▸ Devices mounts the "Notifications on this device" card (PushCard) after the
// device list, feeds it the push service from THIS device's row in the list the panel already
// loads, and hands it the existing pairing flow. The panel is mounted for real; only the
// toast, fetch and lib/push are stubbed.

import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("@protolabsai/ui/overlays", () => ({ useToast: () => () => "t1" }));

import * as push from "../lib/push";
import { DevicesPanel } from "./DevicesPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const now = Math.floor(Date.now() / 1000);
const binding = { vapid_version: 1, generation: 1, created_at: now - 500, renewed_at: now - 3 * 3600 };
const row = (id: string, provider: string) => ({ id, name: id, created_at: 1, last_seen_at: null, push: { ...binding, provider } });

let root: Root;
let host: HTMLDivElement;
let requests: string[];
// What the stubbed server answers: the declared public URL, and whether a pairing can start.
let publicUrl: string;
let pairable: boolean;

beforeEach(() => {
  localStorage.clear();
  requests = [];
  publicUrl = "";
  pairable = false;
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    requests.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/devices") {
      return Response.json({ devices: [row("other", "google"), row("mine", "mozilla")], caller: { scope: "full", device_id: null }, public_console_url: publicUrl });
    }
    if (url === "/api/pairing/cancel") return Response.json({ ok: true });
    if (pairable) {
      const kind = JSON.parse(String(init?.body ?? "{}")).kind === "agent" ? "agent" : "device";
      const ttl = 300;
      return Response.json({
        ok: true, kind, code: "3HV8T-QW6RN", ttl, expires_at: Math.floor(Date.now() / 1000) + ttl,
        hosts: [{ host: "100.64.0.5", kind: "tailnet", base_url: "http://100.64.0.5:7871/app/", url: "http://100.64.0.5:7871/app/#pair=3HV8T-QW6RN", qr: null }],
      });
    }
    // Pairing start: refused, which is enough to prove the card reached the existing flow.
    return Response.json({ error: "not reachable", available: [] }, { status: 409 });
  }));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount() {
  await act(async () => root.render(h(DevicesPanel)));
  for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve(); });
}
const card = () => host.querySelector<HTMLElement>('[data-testid="push-card"]')!;

it("renders the card after the device list, with this device's own push service", async () => {
  localStorage.setItem("protoagent.deviceToken", "own-token");
  localStorage.setItem("protoagent.deviceId", "mine");
  vi.spyOn(push, "pushState").mockResolvedValue("enrolled");
  vi.spyOn(push, "pushStatus").mockResolvedValue({ public_key: "k", version: 1, binding, renew: null });
  await mount();
  expect(host.querySelector('[data-testid="push-toggle"]')).toBeNull();
  expect(card().textContent).toContain("Push service: Mozilla");
  expect(card().textContent).toContain("Renewed 3 hours ago");
  const list = host.querySelector(".devices-list")!;
  expect(list.compareDocumentPosition(card()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("a shared sign-in's Pair this device starts the existing device pairing", async () => {
  vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  await mount();
  const pair = [...card().querySelectorAll("button")].find((b) => b.textContent === "Pair this device")!;
  await act(async () => pair.click());
  expect(requests).toContain("POST /api/pairing/start");
});

// ── The "Phone not on Tailscale?" hint: only while a device pairing is in progress ──────────

const hint = () => host.querySelector<HTMLElement>('[data-testid="public-console-hint"]');
const warning = () => host.querySelector<HTMLElement>('[data-testid="public-console-warning"]');
async function press(label: string) {
  const btn = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === label)!;
  await act(async () => btn.click());
  for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve(); });
}

it("shows no public-URL hint and no config key while no pairing is in progress", async () => {
  vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  await mount();
  expect(hint()).toBeNull();
  expect(warning()).toBeNull();
  expect(host.textContent).not.toContain("network.public_console_url");
});

it("shows the hint, in plain words with the guide link, while a device pairing is in progress", async () => {
  vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  pairable = true;
  await mount();
  await press("Add a device");
  expect(host.querySelector('[data-testid="device-pair-code"]')).not.toBeNull();
  expect(hint()!.textContent).toBe("Phone not on Tailscale? Use your own HTTPS address instead. Set up a public URL");
  const link = hint()!.querySelector("a")!;
  expect(link.textContent).toBe("Set up a public URL");
  expect(link.getAttribute("href")).toMatch(/guides\/phone-access#without-tailscale$/);
  expect(host.textContent).not.toContain("network.public_console_url");
  expect(host.textContent).not.toContain("nginx");
  expect(warning()).toBeNull();

  // Cancelling ends the pairing and the hint with it; an agent pairing is not a phone.
  await press("Cancel");
  expect(hint()).toBeNull();
  await press("Pair an agent");
  expect(host.querySelector('[data-testid="agent-pair-code"]')).not.toBeNull();
  expect(hint()).toBeNull();
});

it("keeps the public-console warning, and no hint, when a public URL is set", async () => {
  vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  publicUrl = "https://agent.example.com";
  pairable = true;
  await mount();
  expect(warning()!.textContent).toContain("This console is public at https://agent.example.com.");
  expect(warning()!.textContent).toContain("How to put Access in front of it");
  expect(hint()).toBeNull();
  await press("Add a device");
  expect(host.querySelector('[data-testid="device-pair-code"]')).not.toBeNull();
  expect(warning()).not.toBeNull();
  expect(hint()).toBeNull();
});
