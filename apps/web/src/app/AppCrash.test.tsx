import { act, Component, createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setDevHooksEnabled } from "../lib/storage";
import { AppCrash, ForcedQuotaCrash, MIN_FREED_BYTES, freeTranscriptSpace, resetChatData } from "./AppCrash";

// ADR 0114 D6 — the crash page's quota recovery. "Free up space & reload" clears ONLY the
// localStorage transcript categories (by exact registry pattern), stops the crashed page's
// chat flush from writing them back, tells other tabs, and — when that didn't free enough to
// matter — lists the largest keys instead of reloading straight back into the crash.

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type G = { __protoagentNoFlush?: boolean; __protoagentForceQuotaCrash?: boolean };

const KEEP = {
  "protoagent.chat.sessions.dismissed": '["a"]',
  "protoagent.chat.sessions:gymBro.dismissed": '["b"]',
  "protoagent.authToken": "tok",
  "protoagent.deviceId": "dev",
  "protoagent.tenant.uid": "uid",
  "pl-theme": '{"mode":"dark"}',
  "protoagent.ui": '{"state":{}}',
  "protoagent.ui:gymBro": '{"state":{}}',
  "proto:uislice:dev-flags": "{}",
  "protoagent.keybindings": "{}",
  "some.plugin.cache": "plugin data", // unregistered — counted, never touched
};

const TRANSCRIPTS = [
  "protoagent.chat.sessions",
  "protoagent.chat.sessions:gymBro",
  "protoagent.palette.chat",
  "protoagent.palette.chat:gymBro",
  "protoagent.palette.chat:gymBro:dm:mothership",
  "protoagent.palette.chat:dm:mothership",
];

function seed(transcriptChars: number) {
  for (const [k, v] of Object.entries(KEEP)) window.localStorage.setItem(k, v);
  for (const k of TRANSCRIPTS) window.localStorage.setItem(k, "x".repeat(transcriptChars));
}

const quotaError = () => Object.assign(new Error("The quota has been exceeded."), { name: "QuotaExceededError" });

let posted: unknown[];

beforeEach(() => {
  window.localStorage.clear();
  delete (globalThis as G).__protoagentNoFlush;
  posted = [];
  class FakeChannel {
    constructor(public name: string) {}
    postMessage(m: unknown) {
      posted.push({ name: this.name, m });
    }
    close() {}
  }
  vi.stubGlobal("BroadcastChannel", FakeChannel);
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (globalThis as G).__protoagentNoFlush;
  delete (globalThis as G).__protoagentForceQuotaCrash;
});

describe("freeTranscriptSpace", () => {
  it("clears only transcript keys — never .dismissed, auth, tenant, theme, layout, or unregistered", () => {
    seed(1000);
    const freed = freeTranscriptSpace();
    for (const k of TRANSCRIPTS) expect(window.localStorage.getItem(k), k).toBeNull();
    for (const [k, v] of Object.entries(KEEP)) expect(window.localStorage.getItem(k), k).toBe(v);
    const expected = TRANSCRIPTS.reduce((n, k) => n + 2 * (k.length + 1000), 0);
    expect(freed).toBe(expected);
  });

  it("stops the chat flush FIRST and broadcasts storage-reset", () => {
    seed(10);
    freeTranscriptSpace();
    expect((globalThis as G).__protoagentNoFlush).toBe(true);
    expect(posted).toEqual([{ name: "protoagent.storage", m: { type: "storage-reset" } }]);
  });

  it("works without BroadcastChannel (feature-detected)", () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    seed(10);
    expect(() => freeTranscriptSpace()).not.toThrow();
    expect(window.localStorage.getItem("protoagent.chat.sessions")).toBeNull();
  });
});

describe("resetChatData (the existing reset, kept)", () => {
  it("still clears every protoagent.chat.sessions* key and stops the flush", () => {
    seed(10);
    resetChatData();
    expect(window.localStorage.getItem("protoagent.chat.sessions")).toBeNull();
    expect(window.localStorage.getItem("protoagent.chat.sessions:gymBro")).toBeNull();
    expect(window.localStorage.getItem("protoagent.authToken")).toBe("tok");
    expect((globalThis as G).__protoagentNoFlush).toBe(true);
  });
});

describe("<AppCrash> on a quota error", () => {
  let container: HTMLElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const button = (label: RegExp) =>
    [...container.querySelectorAll("button")].find((b) => label.test(b.textContent ?? "")) as HTMLButtonElement | undefined;

  it("offers Free up space only for quota errors", () => {
    act(() => root.render(h(AppCrash, { error: new Error("boom"), reload: vi.fn() })));
    expect(button(/Free up space/)).toBeUndefined();
    expect(button(/Reset chat data/)).toBeDefined();
    act(() => root.render(h(AppCrash, { error: quotaError(), reload: vi.fn() })));
    expect(button(/Free up space/)).toBeDefined();
    // Firefox's shape too.
    act(() => root.render(h(AppCrash, { error: Object.assign(new Error("x"), { code: 1014 }), reload: vi.fn() })));
    expect(button(/Free up space/)).toBeDefined();
  });

  it("reloads when it freed enough", () => {
    seed(Math.ceil(MIN_FREED_BYTES / 2 / TRANSCRIPTS.length) + 10);
    const reload = vi.fn();
    act(() => root.render(h(AppCrash, { error: quotaError(), reload })));
    act(() => button(/Free up space/)!.click());
    expect(reload).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem("protoagent.chat.sessions")).toBeNull();
    expect(container.querySelector(".app-crash__keys")).toBeNull();
  });

  it("under 64 KB freed: lists the largest keys (unregistered too) with Clear, instead of reloading", () => {
    seed(10);
    window.localStorage.setItem("design-system.hog", "y".repeat(50_000)); // the real culprit
    const reload = vi.fn();
    act(() => root.render(h(AppCrash, { error: quotaError(), reload })));
    act(() => button(/Free up space/)!.click());
    expect(reload).not.toHaveBeenCalled();
    const rows = [...container.querySelectorAll(".app-crash__keys li code")].map((c) => c.textContent);
    expect(rows[0]).toBe("design-system.hog"); // largest first
    expect(rows).toContain("protoagent.authToken"); // listed (its size counts)…
    // …but credentials and the tenant stamp get no Clear: one click must not lock out a
    // remote operator.
    expect(container.querySelector('button[aria-label="Clear protoagent.authToken"]')).toBeNull();
    expect(container.querySelector('button[aria-label="Clear protoagent.deviceId"]')).toBeNull();
    expect(container.querySelector('button[aria-label="Clear protoagent.tenant.uid"]')).toBeNull();
    expect(container.querySelector('button[aria-label="Clear pl-theme"]')).not.toBeNull();

    const clear = container.querySelector<HTMLButtonElement>('button[aria-label="Clear design-system.hog"]')!;
    act(() => clear.click());
    expect(window.localStorage.getItem("design-system.hog")).toBeNull();
    expect(window.localStorage.getItem("protoagent.authToken")).toBe("tok");
    const after = [...container.querySelectorAll(".app-crash__keys li code")].map((c) => c.textContent);
    expect(after).not.toContain("design-system.hog");
  });
});

describe("ForcedQuotaCrash (e2e hook)", () => {
  let container: HTMLElement;
  let root: Root;
  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    setDevHooksEnabled(true); // vitest runs as a dev build
  });

  // A tiny boundary so the throw is observable without React's uncaught-error path.
  class Catch extends Component<{ children?: ReactNode }, { err: Error | null }> {
    state = { err: null as Error | null };
    static getDerivedStateFromError(err: Error) {
      return { err };
    }
    render() {
      return this.state.err ? h("p", { id: "caught" }, this.state.err.name) : this.props.children;
    }
  }

  it("is inert by default and throws a quota error when forced", () => {
    act(() => root.render(h(Catch, null, h(ForcedQuotaCrash))));
    expect(container.querySelector("#caught")).toBeNull();
    (globalThis as G).__protoagentForceQuotaCrash = true;
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    act(() => root.render(h(Catch, { key: "again" }, h(ForcedQuotaCrash))));
    err.mockRestore();
    expect(container.querySelector("#caught")?.textContent).toBe("QuotaExceededError");
  });

  it("stays inert in production (dev hooks off), even with the global set", () => {
    setDevHooksEnabled(false);
    (globalThis as G).__protoagentForceQuotaCrash = true;
    act(() => root.render(h(Catch, null, h(ForcedQuotaCrash))));
    expect(container.querySelector("#caught")).toBeNull();
    // …and goes live the moment the channel turns out to be non-prod.
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    act(() => setDevHooksEnabled(true));
    err.mockRestore();
    expect(container.querySelector("#caught")?.textContent).toBe("QuotaExceededError");
  });
});
