import "fake-indexeddb/auto";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import {
  chatStore,
  flushChatPersist,
  initTranscriptBackend,
  transcriptStoreIfOpen,
  transcriptWritesSettled,
} from "../chat/chat-store";
import { TranscriptStore } from "../chat/transcriptStore";
import { api } from "../lib/api";
import { __resetStorageSeamForTests } from "../lib/storage";
import { LAST_AUTO_CLEAR_KEY } from "../lib/storageEviction";
import type { ChatMessage, FleetStatus } from "../lib/types";
import { StoragePanel } from "./StoragePanel";

// Settings ▸ Storage (docs/design/settings-storage.md): the five states the approved preview
// shows (normal, freed, full, plain http, Keep data refused) and the clear actions, including
// "not while a reply runs". The real chat store on fake-indexeddb, the real DS components.

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const pad = (n: number) => "x".repeat(n);
const chat = (id: string, kb: number): ChatMessage[] => [
  { id: `${id}-u`, role: "user", content: "q", status: "done" },
  { id: `${id}-a`, role: "assistant", content: pad(kb * 1000), status: "done", taskId: `${id}-t` },
];

const FLEET: FleetStatus = {
  agents: [
    { name: "protoAgent", id: "main", port: 7870, pid: 1, running: true, bundle: "", host: true },
    { name: "war-boss", label: "warBoss", id: "warboss", port: 7871, pid: 2, running: true, bundle: "" },
    { name: "jobcoach", label: "jobCoach", id: "jobcoach", port: 7872, pid: 3, running: true, bundle: "" },
  ],
};

let root: Root;
let node: HTMLDivElement;
let estimate: { usage: number; quota: number } | null;
let persisted: boolean;
let persistAnswer: boolean;

function stubStorageManager() {
  Object.defineProperty(navigator, "storage", {
    configurable: true,
    value: {
      estimate: estimate ? async () => estimate : undefined,
      persisted: async () => persisted,
      persist: vi.fn(async () => {
        persisted = persistAnswer;
        return persistAnswer;
      }),
    },
  });
}

function setSecure(secure: boolean) {
  Object.defineProperty(window, "isSecureContext", { configurable: true, value: secure });
}

async function seed(slug: string, sessions: { id: string; kb: number; updatedAt?: number }[]) {
  await transcriptStoreIfOpen()!.putSessions(
    slug,
    sessions.map((s) => ({ id: s.id, title: s.id, createdAt: 1, updatedAt: s.updatedAt ?? Date.now(), messages: chat(s.id, s.kb) })),
  );
}

async function render() {
  await act(async () =>
    root.render(
      <QueryClientProvider client={new QueryClient()}>
        <ToastProvider>
          <StoragePanel />
        </ToastProvider>
      </QueryClientProvider>,
    ),
  );
  await settle();
}

async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => await new Promise((r) => setTimeout(r, 0)));
}

const text = () => node.textContent ?? "";
const rowNames = () => [...node.querySelectorAll(".storage-row__label")].map((el) => el.textContent?.trim());
const button = (name: string) =>
  [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => (b.getAttribute("aria-label") ?? b.textContent?.trim()) === name);
async function click(name: string) {
  const b = button(name);
  if (!b) throw new Error(`no button ${name}`);
  await act(async () => b.click());
  await settle();
}
const dialog = () => document.querySelector(".pl-dialog");

beforeAll(async () => {
  window.history.replaceState(null, "", "/app/");
  expect(await initTranscriptBackend()).toBe("idb");
});

beforeEach(async () => {
  for (const s of chatStore.getSnapshot().sessions) chatStore.setSessionStatus(s.id, "idle");
  await transcriptWritesSettled();
  await transcriptStoreIfOpen()!.clear();
  localStorage.clear();
  __resetStorageSeamForTests();
  estimate = { usage: 3_400_000, quota: 60_000_000_000 };
  persisted = false;
  persistAnswer = true;
  stubStorageManager();
  setSecure(true);
  vi.spyOn(api, "fleet").mockResolvedValue(FLEET);
  node = document.createElement("div");
  document.body.append(node);
  root = createRoot(node);
});

afterEach(async () => {
  await act(async () => root.unmount());
  node.remove();
  vi.restoreAllMocks();
});

describe("normal", () => {
  it("a green dot, one sentence, rows by size with real names and neutral badges", async () => {
    await seed("warboss", [{ id: "w1", kb: 40 }, { id: "w2", kb: 40 }]);
    await seed("jobcoach", [{ id: "j1", kb: 20 }]);
    await seed("threedee", [{ id: "t1", kb: 30 }]); // left the fleet
    const host = chatStore.getSnapshot().currentSessionId!;
    await seed("host", [{ id: host, kb: 10 }]);
    await render();

    expect(text()).toContain("Chats are saving in this browser.");
    expect(text()).toContain("3.2 MB used, with plenty of room left.");
    expect(node.querySelector(".pl-callout")).toBeNull();
    // Largest first; the host by its real name, with `this window`.
    expect(rowNames()).toEqual(["warBoss", "threedee", "jobCoach", "protoAgent", "Layout and preferences"]);
    const hostRow = node.querySelector('[data-slug="host"]')!;
    expect(hostRow.textContent).toContain("this window");
    expect(node.querySelector('[data-slug="threedee"]')!.textContent).toContain("not in fleet");
    expect(node.querySelector('[data-slug="warboss"]')!.textContent).toMatch(/2 chats · \d+ KB/);
    expect(button("Clear warBoss's chats")).toBeTruthy();
    // One line offers the agents that left.
    expect(text()).toMatch(/1 agent is no longer in your fleet \(\d+ KB\)\./);
    expect(button("Clear them")).toBeTruthy();
    expect(text()).toContain("Includes your sign-in, so it isn't cleared here.");
    expect(text()).toContain("Nothing has been cleared automatically.");
    expect(text()).toContain("Asks the browser not to clear this console's data on its own, for example when your disk is low.");
    // Section headings are h3s under the panel's h2.
    expect([...node.querySelectorAll("h3")].map((h) => h.textContent)).toEqual(["What's using space", "Keep data", "Last automatic clear"]);
  });

  it("the Last saved tooltip text is in the row's accessible description", async () => {
    await seed("warboss", [{ id: "w1", kb: 5, updatedAt: Date.now() - 3 * 86_400_000 }]);
    await render();
    const meta = node.querySelector<HTMLElement>('[data-slug="warboss"] .storage-row__meta')!;
    expect(meta.tabIndex).toBe(0);
    expect(document.getElementById(meta.getAttribute("aria-describedby")!)!.textContent).toBe("Last saved 3 days ago");
  });
});

describe("freed (after an automatic clear)", () => {
  it("keeps the green dot, adds one sentence, and fills in the last automatic clear", async () => {
    const at = new Date();
    at.setHours(0, 5, 0, 0); // today 00:05, so it's always earlier today
    localStorage.setItem(
      LAST_AUTO_CLEAR_KEY,
      JSON.stringify({
        at: at.getTime(),
        chats: 5,
        bytes: 302 * 1024,
        quick: 0,
        agents: [
          { slug: "threeDee", chats: 4, gone: true },
          { slug: "promptInjection", chats: 1, gone: true },
        ],
      }),
    );
    await render();
    expect(text()).toContain("Chats are saving in this browser.");
    expect(text()).toContain("Made room today at 00:05 by clearing 5 old chats.");
    expect(text()).toContain(
      "Today at 00:05: 5 chats from threeDee (4 chats) and promptInjection (1 chat), which are no longer in your fleet. 302 KB freed.",
    );
    expect(node.querySelector(".pl-callout")).toBeNull();
  });
});

describe("full (saving fails) with a reply running", () => {
  it("a warning callout with Free up space; this agent's Clear waits for the reply", async () => {
    await seed("threedee", [{ id: "t1", kb: 30 }]);
    const id = chatStore.getSnapshot().currentSessionId!;
    await seed("host", [{ id, kb: 10 }]);
    // Every save fails: the store is full and nothing may be cleared (the fleet is unknown).
    const put = vi.spyOn(TranscriptStore.prototype, "putSessions").mockRejectedValue(new DOMException("full", "QuotaExceededError"));
    chatStore.updateMessages(id, chat(id, 10));
    act(() => chatStore.setSessionStatus(id, "streaming"));
    flushChatPersist();
    await transcriptWritesSettled();
    await render();

    const callout = node.querySelector(".pl-callout--warning")!;
    expect(callout).toBeTruthy();
    expect(callout.querySelector(".pl-callout__title")).toBeNull();
    expect(callout.textContent).toContain("New messages aren't being saved in this browser.");
    expect(callout.textContent).toContain(
      "Chats keep working, and the server keeps the last 24 hours of each one. Free up space to start saving again.",
    );
    expect(button("Free up space")).toBeTruthy();
    // In `failing`, Free up space covers the agents that left: no separate line.
    expect(button("Clear them")).toBeUndefined();
    expect(button("Clear protoAgent's chats")!.disabled).toBe(true);
    expect(text()).toContain("Replying now. You can clear this after the reply finishes.");
    expect(text()).toContain("Saving: failing.");

    // The reply ends: Clear comes back without a reload.
    act(() => chatStore.setSessionStatus(id, "idle"));
    await settle();
    expect(button("Clear protoAgent's chats")!.disabled).toBe(false);
    expect(text()).not.toContain("Replying now.");

    // Free up space: the dialog says the order, then the save goes through and the page is ok.
    await transcriptWritesSettled();
    put.mockRestore();
    await click("Free up space");
    expect(dialog()!.textContent).toContain("Free up space?");
    expect(dialog()!.textContent).toContain("Clears old chats from this browser until new messages fit, in this order:");
    expect(dialog()!.textContent).toContain("protoAgent's oldest chats");
    const freeButtons = [...dialog()!.querySelectorAll("button")].filter((b) => b.textContent === "Free up space");
    await act(async () => freeButtons[0].click());
    await settle();
    await transcriptWritesSettled();
    await settle();
    expect(node.querySelector(".pl-callout--warning")).toBeNull();
    expect(text()).toContain("Chats are saving in this browser.");
    expect(document.body.textContent).toContain("New messages are saving again.");
  });
});

describe("plain http", () => {
  it("no browser estimate, and Keep data disabled with the reason", async () => {
    estimate = null;
    setSecure(false);
    stubStorageManager();
    await seed("warboss", [{ id: "w1", kb: 20 }]);
    await render();
    expect(text()).toMatch(/About \d+ KB saved\./);
    expect(button("Keep data")!.disabled).toBe(true);
    expect(text()).toContain("Only works on a secure (https) address or in the desktop app. This console is on plain http.");
    expect(text()).toContain("Browser's estimate: unavailable (needs a secure address and Safari 17 or later).");
    expect(text()).toContain("Persistent storage: not available on this address.");
  });
});

describe("Keep data", () => {
  it("refused by the browser: plain copy, the button stays enabled and asks again", async () => {
    persistAnswer = false;
    await render();
    await click("Keep data");
    expect(text()).toContain(
      "This browser chose not to keep it this time. That's the browser's call and nothing is wrong: your chats save as usual. Browsers often say yes later, for example once you've used the console for a while or bookmarked it.",
    );
    expect(button("Keep data")!.disabled).toBe(false);
    expect(text()).toContain("Persistent storage: requested and not granted.");
    persistAnswer = true;
    await click("Keep data");
    expect(text()).toContain("On");
    expect(text()).toContain(
      "The browser won't clear this console's data on its own. The console still clears old chats itself when it runs out of room.",
    );
    expect(button("Keep data")).toBeUndefined();
  });

  it("already granted reads as On", async () => {
    persisted = true;
    await render();
    expect(node.querySelector(".storage-on")!.textContent).toBe("On");
  });
});

describe("clearing", () => {
  it("one agent: the dialog says what goes and what stays; the row leaves and focus moves on", async () => {
    await seed("warboss", [{ id: "w1", kb: 40 }, { id: "w2", kb: 40 }]);
    await seed("jobcoach", [{ id: "j1", kb: 20 }]);
    await render();
    await act(async () => button("Clear warBoss's chats")!.focus());
    await click("Clear warBoss's chats");
    expect(dialog()!.textContent).toContain("Clear warBoss's chats from this browser?");
    expect(dialog()!.textContent).toMatch(/Removes 2 saved chats \(\d+ KB\) from this browser\./);
    expect(dialog()!.textContent).toContain(
      "Stays: the server's copy of the last 24 hours of each chat, warBoss's memory and settings, and anything on your other devices. Chats open in a tab stay too.",
    );
    const confirm = [...dialog()!.querySelectorAll("button")].find((b) => b.textContent === "Clear chats")!;
    expect(confirm.className).toContain("pl-btn--danger");
    const retire = vi.spyOn(api, "deleteChatSession");
    await act(async () => confirm.click());
    await settle();

    expect(node.querySelector('[data-slug="warboss"]')).toBeNull();
    expect((await transcriptStoreIfOpen()!.loadAgent("warboss")).sessions).toEqual([]);
    expect((await transcriptStoreIfOpen()!.loadAgent("jobcoach")).sessions).toHaveLength(1);
    expect(retire).not.toHaveBeenCalled(); // this browser's copy only
    expect(document.body.textContent).toMatch(/Cleared warBoss's chats from this browser\. \d+ KB freed\./);
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Clear jobCoach's chats");
    // A manual clear never becomes the "last automatic clear".
    expect(text()).toContain("Nothing has been cleared automatically.");
  });

  it("agents no longer in the fleet, all at once", async () => {
    await seed("threedee", [{ id: "t1", kb: 30 }, { id: "t2", kb: 10 }]);
    await seed("promptinjection", [{ id: "p1", kb: 10 }]);
    await seed("warboss", [{ id: "w1", kb: 10 }]);
    await render();
    expect(text()).toMatch(/2 agents are no longer in your fleet \(\d+ KB\)\./);
    await click("Clear them");
    expect(dialog()!.textContent).toContain("Clear agents no longer in your fleet?");
    expect(dialog()!.textContent).toMatch(/Removes 3 saved chats \(\d+ KB\) from threedee and promptinjection\./);
    await act(async () => [...dialog()!.querySelectorAll("button")].find((b) => b.textContent === "Clear chats")!.click());
    await settle();
    expect(rowNames()).toEqual(["warBoss", "Layout and preferences"]);
    expect(button("Clear them")).toBeUndefined();
  });

  it("quick chats", async () => {
    await transcriptStoreIfOpen()!.putThread("protoagent.palette.chat:warboss", chat("p", 5));
    await transcriptStoreIfOpen()!.putThread("protoagent.palette.chat:dm:jobcoach", chat("d", 5));
    await render();
    expect(node.querySelector('[data-slug="quick"]')!.textContent).toMatch(/2 threads · \d+ KB/);
    await click("Clear quick chats");
    expect(dialog()!.textContent).toContain("Clear quick chats from this browser?");
    expect(dialog()!.textContent).toMatch(/Removes 2 quick chat threads \(\d+ KB\) from the command palette and direct messages\./);
    await act(async () => [...dialog()!.querySelectorAll("button")].find((b) => b.textContent === "Clear quick chats")!.click());
    await settle();
    expect(node.querySelector('[data-slug="quick"]')).toBeNull();
    expect((await transcriptStoreIfOpen()!.loadThreads("protoagent.palette.chat")).size).toBe(0);
    // With nothing left to clear, focus lands on the section heading, never <body>.
    expect(document.activeElement?.textContent).toBe("What's using space");
  });

  it("this window's agent keeps its open chat", async () => {
    const id = chatStore.getSnapshot().currentSessionId!;
    await seed("host", [{ id, kb: 10 }, { id: "closed-1", kb: 10, updatedAt: 1 }]);
    await render();
    expect(node.querySelector('[data-slug="host"]')!.textContent).toContain("2 chats");
    await click("Clear protoAgent's chats");
    expect(dialog()!.textContent).toMatch(/Removes 1 saved chat \(\d+ KB\) from this browser\./);
    await act(async () => [...dialog()!.querySelectorAll("button")].find((b) => b.textContent === "Clear chats")!.click());
    await settle();
    expect((await transcriptStoreIfOpen()!.loadAgent("host")).sessions.map((s) => s.id)).toEqual([id]);
    expect(node.querySelector('[data-slug="host"]')!.textContent).toContain("1 chat ·");
  });
});
