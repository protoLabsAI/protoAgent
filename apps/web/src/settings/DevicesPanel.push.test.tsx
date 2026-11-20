// Settings ▸ Devices mounts the "Notifications on this device" card (PushCard) at the top of
// the page, feeds it the push service from THIS device's row in the list the panel already
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

beforeEach(() => {
  localStorage.clear();
  requests = [];
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    requests.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/devices") {
      return Response.json({ devices: [row("other", "google"), row("mine", "mozilla")], caller: { scope: "full", device_id: null }, public_console_url: "" });
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

it("renders the card above the device list, with this device's own push service", async () => {
  localStorage.setItem("protoagent.deviceToken", "own-token");
  localStorage.setItem("protoagent.deviceId", "mine");
  vi.spyOn(push, "pushState").mockResolvedValue("enrolled");
  vi.spyOn(push, "pushStatus").mockResolvedValue({ public_key: "k", version: 1, binding, renew: null });
  await mount();
  expect(host.querySelector('[data-testid="push-toggle"]')).toBeNull();
  expect(card().textContent).toContain("Push service: Mozilla");
  expect(card().textContent).toContain("Renewed 3 hours ago");
  const list = host.querySelector(".devices-list")!;
  expect(card().compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("a shared sign-in's Pair this device starts the existing device pairing", async () => {
  vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  await mount();
  const pair = [...card().querySelectorAll("button")].find((b) => b.textContent === "Pair this device")!;
  await act(async () => pair.click());
  expect(requests).toContain("POST /api/pairing/start");
});
