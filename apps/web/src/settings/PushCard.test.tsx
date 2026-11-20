// Settings ▸ Devices ▸ "Notifications on this device" (ADR 0135 D10 / W2e). The card is
// mounted for real with the DS button; only lib/push, the desktop probe and the browser
// environment are stubbed.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as desktop from "../lib/api/desktop";
import * as push from "../lib/push";
import { ANNOUNCE_GAP_MS, PushCard, renewedAgo, type PushCardProps } from "./PushCard";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Status = Awaited<ReturnType<typeof push.pushStatus>>;
const NOW = Math.floor(Date.now() / 1000);
const status = (over: Partial<Status> = {}): Status => ({ public_key: "k", version: 1, binding: null, renew: null, ...over });
const bound = status({ binding: { vapid_version: 1, generation: 1, created_at: NOW - 9 * 86400, renewed_at: NOW - 2 * 86400 - 60 } });
const refused = (code: string, http = 409) => new push.PushError(http, code);

const TROUBLE = "Couldn't check notifications on this device.";
const ON = "Notifications are on for this device.";
const OFF = "Notifications are off for this device.";

let root: Root;
let node: HTMLDivElement;
let said: string[];
let watcher: MutationObserver | null;

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("protoagent.deviceToken", "own-token");
  vi.stubGlobal("isSecureContext", true);
  said = [];
  watcher = null;
  node = document.createElement("div");
  document.body.append(node);
  root = createRoot(node);
});
afterEach(async () => {
  watcher?.disconnect();
  await act(async () => root.unmount());
  node.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Let the card's promise chains and the status line's clear-then-set gap run out. */
async function settle() {
  await act(async () => { await new Promise((done) => setTimeout(done, ANNOUNCE_GAP_MS + 15)); });
}
function mock(state: push.PushState | Error, server: Status | Error = status()) {
  const read = vi.spyOn(push, "pushState");
  if (state instanceof Error) read.mockRejectedValue(state); else read.mockResolvedValue(state);
  const get = vi.spyOn(push, "pushStatus");
  if (server instanceof Error) get.mockRejectedValue(server); else get.mockResolvedValue(server);
  return { read, get };
}
async function render(props: PushCardProps = {}) {
  await act(async () => root.render(<PushCard {...props} />));
  // Every sentence the live region ever holds, in order: the test's record of what was spoken.
  const line = statusLine();
  watcher = new MutationObserver(() => { if (line.textContent) said.push(line.textContent); });
  watcher.observe(line, { childList: true, characterData: true, subtree: true });
  await settle();
}
const card = () => node.querySelector<HTMLElement>('[data-testid="push-card"]')!;
const stateText = () => node.querySelector('[data-testid="push-state"]')?.textContent ?? "";
const statusLine = () => node.querySelector<HTMLElement>('[role="status"]')!;
const heading = () => node.querySelector("h3")!;
const refusal = () => node.querySelector('[data-testid="push-refusal"]');
const buttons = () => [...node.querySelectorAll("button")];
const button = (name: string) => buttons().find((b) => b.textContent?.includes(name));
async function click(name: string) {
  await act(async () => button(name)!.click());
  await settle();
}

describe("the ten states", () => {
  it("1: the desktop app needs no enrollment and offers nothing to press", async () => {
    vi.spyOn(desktop, "isDesktopWebview").mockReturnValue(true);
    const { read } = mock("not enrolled");
    await render();
    expect(stateText()).toContain("Desktop app");
    expect(stateText()).toContain("The desktop app shows alerts while it's running, including when its window is closed. Quitting the app stops them.");
    expect(buttons()).toHaveLength(0);
    expect(read).not.toHaveBeenCalled();
  });

  it("2: a plain-http console links to the first HTTPS address, and hides the link when there is none", async () => {
    vi.stubGlobal("isSecureContext", false);
    mock("unsupported");
    localStorage.setItem("protoagent.deviceAddresses", JSON.stringify([
      { kind: "lan", origin: "http://192.168.1.5:7870" },
      { kind: "tailnet_https", origin: "https://box.tail1234.ts.net" },
    ]));
    await render();
    expect(stateText()).toContain("Not available on this address");
    expect(stateText()).toContain("Notifications need an HTTPS address.");
    const link = node.querySelector("a")!;
    expect(link.textContent).toBe("Open this agent's HTTPS address");
    expect(link.getAttribute("href")).toBe("https://box.tail1234.ts.net/app/");

    localStorage.removeItem("protoagent.deviceAddresses");
    await act(async () => root.render(<PushCard key="again" />));
    expect(stateText()).toContain("Notifications need an HTTPS address.");
    expect(node.querySelector("a")).toBeNull();
  });

  it("3: an iPhone browser tab gets the Home Screen steps, in order", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)");
    mock("unsupported");
    await render();
    expect(stateText()).toContain("On iPhone, notifications work only from the Home Screen app.");
    expect([...node.querySelectorAll("ol > li")].map((li) => li.textContent)).toEqual([
      "Tap Share, then Add to Home Screen.",
      "Open protoAgent from your Home Screen.",
      "If it asks, pair this phone again there.",
      "Tap Turn on notifications.",
    ]);
    expect(stateText()).toContain("Needs iOS 16.4 or later.");
    expect(buttons()).toHaveLength(0);
  });

  it("3 does not apply to the installed Home Screen app", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)");
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    mock("not enrolled");
    await render();
    expect(button("Turn on notifications")).toBeTruthy();
  });

  it("4: a browser without push says so", async () => {
    mock("unsupported");
    await render();
    expect(stateText()).toContain("Not supported");
    expect(stateText()).toContain("This browser can't get notifications.");
    expect(buttons()).toHaveLength(0);
  });

  it("5: a shared sign-in is sent to the pairing flow", async () => {
    mock("not enrolled", refused("not_a_device", 403));
    const onPair = vi.fn();
    await render({ onPair });
    expect(stateText()).toContain("Shared sign-in");
    expect(stateText()).toContain("Pair this browser to get notifications.");
    await click("Pair this device");
    expect(onPair).toHaveBeenCalledOnce();
  });

  it("5 also covers a console holding no device token, without asking the agent", async () => {
    localStorage.removeItem("protoagent.deviceToken");
    const { get } = mock("not enrolled");
    await render({ onPair: vi.fn() });
    expect(stateText()).toContain("Shared sign-in");
    expect(get).not.toHaveBeenCalled();
  });

  it("6: a blocked browser gets a hint and is never asked again", async () => {
    mock("denied");
    await render();
    expect(stateText()).toContain("Notifications are blocked in this browser's settings.");
    expect(stateText()).toContain("allow notifications");
    expect(buttons()).toHaveLength(0);
  });

  it.each([
    ["vapid_key", "This agent changed its notification key."],
    ["handoff", "This phone moved to a new address, which needs its own notifications."],
    ["idle", "This device went unused for too long."],
  ] as const)("7: renew (%s) gives its reason and one primary action", async (renew, reason) => {
    // First match wins: a renewal is owed even while the browser still looks enrolled.
    mock("enrolled", { ...bound, renew });
    await render();
    expect(stateText()).toContain("Renew on this device");
    expect(stateText()).toContain(reason);
    expect(buttons().map((b) => b.textContent?.trim())).toEqual(["Renew notifications"]);
  });

  it("8: off offers to turn on", async () => {
    mock("not enrolled");
    await render();
    expect(stateText()).toContain("Off on this device");
    expect(stateText()).toContain("Get alerts on this device even when the console is closed.");
    expect(buttons().map((b) => b.textContent?.trim())).toEqual(["Turn on notifications"]);
  });

  it("9: on shows the push service from this device's row and when it was renewed", async () => {
    mock("enrolled", bound);
    await render({ provider: "apple" });
    expect(node.querySelector(".pl-badge")?.textContent).toBe("On");
    expect(stateText()).toContain("Push service: Apple");
    expect(stateText()).toContain("Renewed 2 days ago");
    expect(buttons().map((b) => b.textContent?.trim())).toEqual(["Turn off on this device"]);
  });

  it.each([[undefined], ["acme-push"]])("9 hides the push service line when it is %s", async (provider) => {
    mock("enrolled", bound);
    await render({ provider });
    expect(stateText()).not.toContain("Push service");
  });

  it.each([
    ["pushState rejects", new Error("offline"), status()],
    ["the agent answers 503", "enrolled", refused("unavailable", 503)],
  ] as const)("10: %s reads as couldn't check, never as off", async (_name, state, server) => {
    mock(state, server);
    await render();
    expect(stateText()).toBe(TROUBLE);
    expect(buttons().map((b) => b.textContent?.trim())).toEqual(["Try again"]);
  });

  it("says how long ago in whole units", () => {
    const now = 1_000_000_000;
    expect(renewedAgo(now / 1000 - 5, now)).toBe("just now");
    expect(renewedAgo(now / 1000 - 60, now)).toBe("1 minute ago");
    expect(renewedAgo(now / 1000 - 3 * 3600, now)).toBe("3 hours ago");
    expect(renewedAgo(now / 1000 - 86400, now)).toBe("1 day ago");
  });
});

describe("turning on and renewing", () => {
  it("calls enrollPush from the tap itself, before anything is awaited", async () => {
    const order: string[] = [];
    const { get } = mock("not enrolled");
    await render();
    get.mockImplementation(async () => { order.push("pushStatus"); return bound; });
    const renew = vi.spyOn(push, "renewPush").mockImplementation(async () => { order.push("renewPush"); return "enrolled"; });
    const enroll = vi.spyOn(push, "enrollPush").mockImplementation(async () => { order.push("enrollPush"); return "enrolled"; });
    await act(async () => {
      button("Turn on notifications")!.click();
      // Still inside the click's own task: no promise has had a chance to settle.
      expect(order).toEqual(["enrollPush"]);
    });
    await settle();
    expect(enroll).toHaveBeenCalledOnce();
    expect(renew).not.toHaveBeenCalled();
    expect(order).toEqual(["enrollPush", "pushStatus"]);
  });

  it("enrolled: shows On, says so, tells the panel, and moves focus to the heading", async () => {
    const { get } = mock("not enrolled");
    const onChanged = vi.fn();
    await render({ provider: "google", onChanged });
    get.mockResolvedValue(bound);
    let granted!: (state: push.PushState) => void;
    vi.spyOn(push, "enrollPush").mockReturnValue(new Promise((resolve) => { granted = resolve; }));
    await click("Turn on notifications");
    // In flight: the button keeps its name, shows busy, and the status line says what's happening.
    expect(button("Turn on notifications")!.getAttribute("aria-busy")).toBe("true");
    expect(statusLine().textContent).toBe("Turning on…");
    await act(async () => granted("enrolled"));
    await settle();
    expect(stateText()).toContain("Push service: Google");
    expect(stateText()).toContain("Renewed 2 days ago");
    expect(document.activeElement).toBe(heading());
    expect(onChanged).toHaveBeenCalledOnce();
    expect(said).toEqual(["Turning on…", ON]);
  });

  it("denied: shows the blocked state and moves focus to the heading", async () => {
    mock("not enrolled");
    await render();
    vi.spyOn(push, "enrollPush").mockResolvedValue("denied");
    await click("Turn on notifications");
    expect(stateText()).toContain("Notifications are blocked in this browser's settings.");
    expect(buttons()).toHaveLength(0);
    expect(document.activeElement).toBe(heading());
  });

  it("a dismissed prompt changes nothing, says nothing, and keeps focus on the button", async () => {
    mock("not enrolled");
    await render();
    vi.spyOn(push, "enrollPush").mockResolvedValue("not enrolled");
    await click("Turn on notifications");
    expect(stateText()).toContain("Off on this device");
    expect(refusal()).toBeNull();
    expect(statusLine().textContent).toBe("");
    expect(document.activeElement).toBe(button("Turn on notifications"));
  });

  it.each([
    ["endpoint_in_use", "Another paired device already uses this browser's notifications. Turn them off there first."],
    ["not_enrollable", "This device can't change notifications right now. Reload the console and try again."],
    ["invalid_endpoint", "This browser's push service isn't supported yet."],
  ])("refusal %s: its copy sits under the button, is announced, and focus stays on the button", async (code, copy) => {
    mock("not enrolled");
    await render();
    vi.spyOn(push, "enrollPush").mockRejectedValue(refused(code));
    await click("Turn on notifications");
    expect(stateText()).toContain("Off on this device");
    expect(refusal()?.textContent?.trim()).toBe(copy);
    expect(refusal()?.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    expect(statusLine().textContent).toBe(copy);
    expect(document.activeElement).toBe(button("Turn on notifications"));
  });

  it("a repeated refusal is announced twice", async () => {
    const copy = "This browser's push service isn't supported yet.";
    mock("not enrolled");
    await render();
    vi.spyOn(push, "enrollPush").mockRejectedValue(refused("invalid_endpoint"));
    await click("Turn on notifications");
    await click("Turn on notifications");
    // An instant refusal replaces "Turning on…" before it is spoken; the refusal itself is
    // cleared and set again, so the same words reach the screen reader both times.
    expect(said).toEqual([copy, copy]);
  });

  it("stale_vapid_version: retries once without a prompt, and succeeds quietly", async () => {
    const { get } = mock("not enrolled");
    await render();
    get.mockResolvedValue(bound);
    vi.spyOn(push, "enrollPush").mockRejectedValue(refused("stale_vapid_version"));
    const renew = vi.spyOn(push, "renewPush").mockResolvedValue("enrolled");
    await click("Turn on notifications");
    expect(renew).toHaveBeenCalledOnce();
    expect(node.querySelector(".pl-badge")?.textContent).toBe("On");
    expect(refusal()).toBeNull();
  });

  it("stale_vapid_version twice lands in couldn't check, on the heading", async () => {
    mock("not enrolled");
    await render();
    const enroll = vi.spyOn(push, "enrollPush").mockRejectedValue(refused("stale_vapid_version"));
    const renew = vi.spyOn(push, "renewPush").mockRejectedValue(refused("stale_vapid_version"));
    await click("Turn on notifications");
    expect(enroll).toHaveBeenCalledOnce();
    expect(renew).toHaveBeenCalledOnce();
    expect(stateText()).toBe(TROUBLE);
    expect(statusLine().textContent).toBe(TROUBLE);
    expect(document.activeElement).toBe(heading());
  });

  it("any other failure lands in couldn't check, on the heading", async () => {
    mock("not enrolled");
    await render();
    vi.spyOn(push, "enrollPush").mockRejectedValue(new Error("offline"));
    await click("Turn on notifications");
    expect(stateText()).toBe(TROUBLE);
    expect(button("Try again")).toBeTruthy();
    expect(document.activeElement).toBe(heading());
  });

  it("renew announces Renewing… and keeps a refusal in the renew state", async () => {
    mock("not enrolled", status({ renew: "idle" }));
    await render();
    let refuse!: (err: Error) => void;
    vi.spyOn(push, "enrollPush").mockReturnValue(new Promise((_, reject) => { refuse = reject; }));
    await click("Renew notifications");
    expect(statusLine().textContent).toBe("Renewing…");
    await act(async () => refuse(refused("endpoint_in_use")));
    await settle();
    expect(stateText()).toContain("Renew on this device");
    expect(refusal()?.textContent).toContain("Another paired device already uses");
    expect(said).toEqual(["Renewing…", "Another paired device already uses this browser's notifications. Turn them off there first."]);
    expect(document.activeElement).toBe(button("Renew notifications"));
  });

  it("the permission prompt closing (a focus event) can't discard the enrollment in flight", async () => {
    const { read, get } = mock("not enrolled");
    await render();
    get.mockResolvedValue(bound);
    let granted!: (state: push.PushState) => void;
    vi.spyOn(push, "enrollPush").mockReturnValue(new Promise((resolve) => { granted = resolve; }));
    await click("Turn on notifications");
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(read).toHaveBeenCalledOnce(); // no read started mid-action
    await act(async () => granted("enrolled"));
    await settle();
    expect(node.querySelector(".pl-badge")?.textContent).toBe("On");
  });
});

describe("turning off, checking again, coming back", () => {
  it("turn off: no confirm, shows off, says so, and focuses Turn on notifications", async () => {
    mock("enrolled", bound);
    const onChanged = vi.fn();
    await render({ onChanged });
    const unenroll = vi.spyOn(push, "unenrollPush").mockResolvedValue();
    await click("Turn off on this device");
    expect(unenroll).toHaveBeenCalledOnce();
    expect(stateText()).toContain("Off on this device");
    expect(statusLine().textContent).toBe(OFF);
    expect(document.activeElement).toBe(button("Turn on notifications"));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("a failed turn off is couldn't check, not a guess either way", async () => {
    mock("enrolled", bound);
    await render();
    vi.spyOn(push, "unenrollPush").mockRejectedValue(refused("unavailable", 503));
    await click("Turn off on this device");
    expect(stateText()).toBe(TROUBLE);
    expect(document.activeElement).toBe(heading());
  });

  it("try again, changed: focuses the heading and announces where it landed", async () => {
    const { read, get } = mock(new Error("offline"));
    await render();
    read.mockResolvedValue("enrolled");
    get.mockResolvedValue(bound);
    await click("Try again");
    expect(node.querySelector(".pl-badge")?.textContent).toBe("On");
    expect(document.activeElement).toBe(heading());
    expect(said).toEqual([ON]);
  });

  it("try again, unchanged: keeps focus on Try again and says it again", async () => {
    mock(new Error("offline"));
    await render();
    await click("Try again");
    await click("Try again");
    expect(stateText()).toBe(TROUBLE);
    expect(document.activeElement).toBe(button("Try again"));
    expect(said).toEqual([TROUBLE, TROUBLE]);
  });

  it("re-reads permission when the operator comes back from settings, without speaking", async () => {
    const { read } = mock("denied");
    await render();
    expect(buttons()).toHaveLength(0);
    read.mockResolvedValue("not enrolled");
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    await settle();
    expect(button("Turn on notifications")).toBeTruthy();
    expect(said).toEqual([]);
  });
});

describe("structure and wording", () => {
  it("is a labelled section with a focusable heading and one status line that starts empty", async () => {
    mock("not enrolled");
    await render();
    expect(card().tagName).toBe("SECTION");
    expect(card().getAttribute("aria-labelledby")).toBe(heading().id);
    expect(heading().textContent).toBe("Notifications on this device");
    expect(heading().getAttribute("tabindex")).toBe("-1");
    expect(node.querySelectorAll('[role="status"], [role="alert"], [aria-live]')).toHaveLength(1);
    expect(statusLine().textContent).toBe("");
    expect(node.querySelector('[data-testid="push-state"]')!.closest("[role], [aria-live]")).toBeNull();
    for (const icon of node.querySelectorAll("svg")) expect(icon.getAttribute("aria-hidden")).toBe("true");
  });

  it("keeps What leaves this machine closed, with the four sentences inside", async () => {
    mock("not enrolled");
    await render();
    const details = node.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")!.textContent).toBe("What leaves this machine");
    expect([...details.querySelectorAll("p")].map((p) => p.textContent)).toEqual([
      "Alerts go straight from this machine to your browser's push service: Apple, Google or Mozilla. No protoAgent server sits in between, and you don't need a chat app.",
      'The alert says only "protoAgent needs your attention" and "Open the console to view a decision" (or "an update"). It never includes the question, chat text, job output, names or file paths.',
      "The push service can see when alerts are sent, how big they are and this machine's public IP address.",
      "Opening an alert asks you to sign in to the console. Nothing can be approved from the lock screen.",
    ]);
  });

  it("never claims a push was delivered, read or seen, and never prefixes Warning: or Error:", async () => {
    const texts: string[] = [];
    const grab = () => texts.push(card().textContent ?? "");
    const { read, get } = mock("not enrolled");
    await render();
    grab();
    vi.spyOn(push, "enrollPush").mockRejectedValueOnce(refused("endpoint_in_use")).mockRejectedValueOnce(refused("not_enrollable"))
      .mockRejectedValueOnce(refused("invalid_endpoint")).mockResolvedValueOnce("denied");
    for (let i = 0; i < 4; i++) { await click("Turn on notifications"); grab(); }
    for (const [state, server] of [["enrolled", bound], ["not enrolled", status({ renew: "handoff" })], ["unsupported", status()]] as const) {
      read.mockResolvedValue(state);
      get.mockResolvedValue(server);
      await act(async () => { window.dispatchEvent(new Event("focus")); });
      await settle();
      grab();
    }
    read.mockRejectedValue(new Error("offline"));
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    await settle();
    grab();
    expect(new Set(texts).size).toBe(9);
    for (const text of [...texts, ...said]) {
      expect(text).not.toMatch(/\b(delivered|read|seen)\b/i);
      expect(text).not.toMatch(/\b(warning|error):/i);
    }
  });
});
