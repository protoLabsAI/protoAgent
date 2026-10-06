// #2996 — the "Clear this conversation?" confirm behind ⌘K (chat.clear) and /clear. Both
// entry points park a clear request in the store; ChatSurface folds it into this dialog, so
// the server clear + local updateMessages sequence starts ONLY on confirm. These pin the
// dialog's own contract: the exact copy, the memory switches (harvest — ON by default for an
// ordinary chat since #4053 — and #3493's forget), that cancel/backdrop dismiss without
// confirming, that confirm reports both, that harvest/forget are mutually exclusive, and that
// an incognito chat drops the harvest switch for a "never harvested" note (#4053).
//
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts` only, so elements are built with React.createElement, not JSX). Same pattern as
// app/AuthGate.test.ts, which likewise drives a portaled DS Dialog.
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClearConversationDialog } from "./ClearConversationDialog";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

function mount(node: Parameters<Root["render"]>[0]) {
  act(() => {
    root.render(node);
  });
}

// @protolabsai/ui portals the Dialog to <body>, so its content is a SIBLING of `container`.
function buttonByText(text: string) {
  return [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === text);
}

function harvestInput() {
  return document.body.querySelector<HTMLInputElement>(".chat-delete-harvest .pl-switch__input");
}

function forgetInput() {
  return document.body.querySelector<HTMLInputElement>(".chat-delete-forget .pl-switch__input");
}

describe("ClearConversationDialog (#2996, #4053)", () => {
  it("renders nothing while closed", () => {
    mount(h(ClearConversationDialog, { open: false, onConfirm: () => {}, onCancel: () => {} }));
    expect(document.body.querySelector(".pl-dialog")).toBeNull();
  });

  it("open on a regular chat: shows the exact copy and the Harvest switch ON by default (#4053)", () => {
    mount(h(ClearConversationDialog, { open: true, onConfirm: () => {}, onCancel: () => {} }));
    const dialog = document.body.querySelector(".pl-dialog");
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain("Clear this conversation? This cannot be undone.");
    // Harvest is present and ON by default now (incognito is the keep-out-of-memory switch).
    const harvest = harvestInput();
    expect(harvest).not.toBeNull();
    expect(harvest!.checked).toBe(true);
    expect(document.body.textContent).toMatch(/Harvest/i);
  });

  it("Cancel dismisses WITHOUT confirming (dismissing SHALL NOT delete)", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel }));
    act(() => buttonByText("Cancel")!.click());
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Confirm reports harvest=true when the switch is left untouched (default on, #4053)", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith({ harvest: true, forget: false });
  });

  it("Confirm reports harvest=false once the switch is unticked", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    // A controlled checkbox → click toggles + fires change. Starts on, so this turns it off.
    act(() => harvestInput()!.click());
    expect(harvestInput()!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: false });
  });

  // #3493: the harvest switch never controlled compaction, so the dialog must not imply it
  // did — it says archives may already exist, and offers a separate forget.
  it("says compaction may already have archived the chat, and offers forget OFF by default", () => {
    mount(h(ClearConversationDialog, { open: true, onConfirm: () => {}, onCancel: () => {} }));
    const text = document.body.querySelector(".pl-dialog")!.textContent!;
    expect(text).toContain("Parts of this chat may already be in the knowledge base");
    expect(text).toContain("/compact");
    expect(text).toContain("Clearing the chat leaves them there unless you choose to forget them below.");
    expect(text).toContain("Forget what this chat already saved to memory");
    expect(forgetInput()).not.toBeNull();
    expect(forgetInput()!.checked).toBe(false);
    // Exactly one "harvest" mention on a regular chat: the e2e locator getByText(/Harvest/i) is strict.
    expect(text.match(/harvest/gi)).toHaveLength(1);
  });

  // #4053 mutual exclusion: forget and harvest can't both be on.
  it("ticking forget unticks harvest and confirms forget=true, harvest=false", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    expect(harvestInput()!.checked).toBe(true); // on by default
    act(() => forgetInput()!.click());
    expect(forgetInput()!.checked).toBe(true);
    expect(harvestInput()!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: true });
  });

  it("re-ticking harvest after forget unticks forget (#4053)", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => forgetInput()!.click()); // forget on, harvest off
    act(() => harvestInput()!.click()); // harvest back on → forget off
    expect(harvestInput()!.checked).toBe(true);
    expect(forgetInput()!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: true, forget: false });
  });

  // #4053 incognito: never harvested — no harvest switch, a note in its place, harvest=false.
  it("incognito chat: hides the harvest switch, shows the note, and confirms harvest=false", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, incognito: true, onConfirm, onCancel: () => {} }));
    const text = document.body.querySelector(".pl-dialog")!.textContent!;
    expect(harvestInput()).toBeNull();
    expect(text).toContain("Incognito chat — never harvested into the knowledge base");
    // The forget switch still shows for an incognito chat.
    expect(forgetInput()).not.toBeNull();
    expect(forgetInput()!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: false });
  });

  it("incognito chat: forget still reaches the server as forget=true, harvest stays false", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, incognito: true, onConfirm, onCancel: () => {} }));
    act(() => forgetInput()!.click());
    expect(forgetInput()!.checked).toBe(true);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: true });
  });

  it("re-initialises the switches from the chat's incognito flag each time it reopens (#4053)", () => {
    const onConfirm = vi.fn();
    // Open on a regular chat and flip both away from their defaults.
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => forgetInput()!.click()); // forget on, harvest off
    expect(harvestInput()!.checked).toBe(false);
    expect(forgetInput()!.checked).toBe(true);
    // Close, then reopen on the same regular chat — defaults re-arm (harvest on, forget off).
    mount(h(ClearConversationDialog, { open: false, onConfirm, onCancel: () => {} }));
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    expect(harvestInput()!.checked).toBe(true);
    expect(forgetInput()!.checked).toBe(false);
    // Reopen as incognito — harvest switch gone, forget re-armed off (no stale carryover).
    mount(h(ClearConversationDialog, { open: false, onConfirm, onCancel: () => {} }));
    mount(h(ClearConversationDialog, { open: true, incognito: true, onConfirm, onCancel: () => {} }));
    expect(harvestInput()).toBeNull();
    expect(forgetInput()!.checked).toBe(false);
  });
});
