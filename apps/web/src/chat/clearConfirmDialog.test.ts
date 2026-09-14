// #2996 — the "Clear this conversation?" confirm behind ⌘K (chat.clear) and /clear. Both
// entry points park a clear request in the store; ChatSurface folds it into this dialog, so
// the server clear + local updateMessages sequence starts ONLY on confirm. These pin the
// dialog's own contract: the exact copy, the two opt-in memory switches (harvest, and #3493's
// forget), that cancel/backdrop dismiss without confirming, and that confirm reports both.
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

describe("ClearConversationDialog (#2996)", () => {
  it("renders nothing while closed", () => {
    mount(h(ClearConversationDialog, { open: false, onConfirm: () => {}, onCancel: () => {} }));
    expect(document.body.querySelector(".pl-dialog")).toBeNull();
  });

  it("open: shows the exact copy and the opt-in Harvest checkbox", () => {
    mount(h(ClearConversationDialog, { open: true, onConfirm: () => {}, onCancel: () => {} }));
    const dialog = document.body.querySelector(".pl-dialog");
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain("Clear this conversation? This cannot be undone.");
    // The harvest opt-in is present, and OFF by default (must not silently harvest).
    const harvest = harvestInput();
    expect(harvest).not.toBeNull();
    expect(harvest!.checked).toBe(false);
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

  it("Confirm reports harvest=false when the checkbox is left off", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: false });
  });

  it("Confirm reports harvest=true once the checkbox is ticked", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    // Tick the harvest switch (a controlled checkbox → click toggles + fires change).
    act(() => harvestInput()!.click());
    expect(harvestInput()!.checked).toBe(true);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: true, forget: false });
  });

  // #3493: the harvest switch never controlled compaction, so the dialog must not imply it
  // did — it says archives may already exist, and offers a separate, opt-in forget.
  it("says compaction may already have archived the chat, and offers forget OFF by default", () => {
    mount(h(ClearConversationDialog, { open: true, onConfirm: () => {}, onCancel: () => {} }));
    const text = document.body.querySelector(".pl-dialog")!.textContent!;
    expect(text).toContain("Parts of this chat may already be in the knowledge base");
    expect(text).toContain("/compact");
    expect(text).toContain("Clearing the chat leaves them there unless you choose to forget them below.");
    expect(text).toContain("Forget what this chat already saved to memory");
    expect(forgetInput()).not.toBeNull();
    expect(forgetInput()!.checked).toBe(false);
    // Exactly one "harvest" mention: the e2e locator getByText(/Harvest/i) is strict.
    expect(text.match(/harvest/gi)).toHaveLength(1);
  });

  it("Confirm reports forget=true once the forget switch is ticked, independent of harvest", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => forgetInput()!.click());
    expect(forgetInput()!.checked).toBe(true);
    expect(harvestInput()!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: true });
  });

  it("resets both opt-ins each time it reopens (no stale tick carries over)", () => {
    mount(h(ClearConversationDialog, { open: true, onConfirm: () => {}, onCancel: () => {} }));
    act(() => harvestInput()!.click());
    act(() => forgetInput()!.click());
    expect(harvestInput()!.checked).toBe(true);
    expect(forgetInput()!.checked).toBe(true);
    // Close, then reopen — the effect re-arms the default-off state.
    mount(h(ClearConversationDialog, { open: false, onConfirm: () => {}, onCancel: () => {} }));
    mount(h(ClearConversationDialog, { open: true, onConfirm: () => {}, onCancel: () => {} }));
    expect(harvestInput()!.checked).toBe(false);
    expect(forgetInput()!.checked).toBe(false);
  });
});
