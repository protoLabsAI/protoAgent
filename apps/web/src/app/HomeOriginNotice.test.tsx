// The offline notice for a paired phone whose home origin stopped answering (ADR 0121 D1,
// #4336 S7.5). createRoot/act like the other console UI suites — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { storeDeviceAddresses, storeDeviceCredential } from "../lib/credentials";
import { __resetStorageSeamForTests } from "../lib/storage";
import { HOME_MISSES, HomeOriginNotice } from "./HomeOriginNotice";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const HOME = window.location.origin;
const SET = [
  { kind: "tailnet_https", origin: "https://box.tail1234.ts.net", requirement: "tailscale" },
  { kind: "public", origin: "https://agent.example.com" },
  { kind: "lan", origin: HOME, requirement: "same_network" },
];

let container: HTMLElement;
let root: Root;
let homeUp = true;
const probed: string[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  window.localStorage.clear();
  homeUp = true;
  probed.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      if (init.mode === "no-cors") {
        probed.push(String(url));
        if (String(url).startsWith("https://agent.example.com")) return new Response(null, { status: 200 });
        throw new TypeError("Failed to fetch");
      }
      if (!homeUp) throw new TypeError("Failed to fetch");
      return new Response("ok", { status: 200 });
    }),
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  __resetStorageSeamForTests();
});

async function tick(times: number) {
  for (let i = 0; i < times; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
  }
}

describe("HomeOriginNotice", () => {
  it("renders nothing while the home answers", async () => {
    storeDeviceCredential("tok", "d1");
    storeDeviceAddresses(SET);
    act(() => root.render(h(HomeOriginNotice, { checkMs: 1000 })));
    await tick(4);
    expect(container.querySelector("[data-testid=home-origin-notice]")).toBeNull();
  });

  it("lists the OTHER origins with their requirements once the home stops answering", async () => {
    storeDeviceCredential("tok", "d1");
    storeDeviceAddresses(SET);
    homeUp = false;
    act(() => root.render(h(HomeOriginNotice, { checkMs: 1000 })));
    await tick(HOME_MISSES - 1);
    expect(container.querySelector("[data-testid=home-origin-notice]")).toBeNull(); // one miss is a blip
    await tick(1);
    const notice = container.querySelector("[data-testid=home-origin-notice]");
    expect(notice).not.toBeNull();
    const links = [...notice!.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(links).toEqual(["https://box.tail1234.ts.net/app/", "https://agent.example.com/app/"]); // not the home
    expect(notice!.textContent).toContain("needs the Tailscale app on this phone");
    expect(notice!.textContent).toContain("works from any network");
    expect(notice!.textContent).toContain("Opening one means pairing this phone again there.");
    // Probes are hints: the one that answered says so, the one that didn't says nothing.
    expect(probed.sort()).toEqual(["https://agent.example.com/healthz", "https://box.tail1234.ts.net/healthz"]);
    await tick(1);
    const items = [...notice!.querySelectorAll("li")].map((li) => li.textContent);
    expect(items[1]).toContain("answering");
    expect(items[0]).not.toContain("answering");
    // Back in range: the notice goes.
    homeUp = true;
    await tick(1);
    expect(container.querySelector("[data-testid=home-origin-notice]")).toBeNull();
  });

  it("stays silent for a browser that isn't a paired device, even when the server is down", async () => {
    window.localStorage.setItem("protoagent.deviceAddresses", JSON.stringify(SET));
    homeUp = false;
    act(() => root.render(h(HomeOriginNotice, { checkMs: 1000 })));
    await tick(4);
    expect(container.querySelector("[data-testid=home-origin-notice]")).toBeNull();
  });
});
